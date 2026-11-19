"""Publish dumps by pushing them to bare git repositories served by cgit.

Repositories live at ``<REPO_ROOT>/dumps/<subgroup>/<repo>.git``. Existing
ones may be symlinks to other storage and owned by another user.
"""

import os
import re
import secrets
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote, urlparse

import httpx
from rich.console import Console

from dumpyarabot.config import settings
from dumpyarabot.message_formatting import format_channel_notification_message
from dumpyarabot.process_utils import (
    FIVE_MINUTES,
    HALF_HOUR,
    ONE_HOUR,
    ProcessException,
    run_command,
    run_git_command,
)

console = Console()

# Names come from firmware properties, so they are untrusted: each must be a
# single plain path component (no "/", ".", ".." or hidden names).
REPO_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Only receive-pack needs safe.directory: it is the one command that finds the
# target repo by itself (from the path it is given) and so checks ownership,
# and repos may be owned by another user. Commands given an explicit
# --git-dir skip that check, and push only opens the work dir. A local push
# clears GIT_CONFIG_PARAMETERS for receive-pack, so the -c goes into its
# command (through a shell, hence the quoting). receive.autogc=false stops
# pushes from starting a repack of a large repo; maintenance is left to the
# server. core.hooksPath=/dev/null keeps hooks in a repo owned by someone else
# from running as the bot.
RECEIVE_PACK_COMMAND = (
    "git -c 'safe.directory=*' -c receive.autogc=false"
    " -c core.hooksPath=/dev/null receive-pack"
)


class RepoPublishError(Exception):
    """Publishing a dump to its repository failed.

    The message names only the failed step: it is shown to Telegram users,
    while the git command and paths are logged on the worker instead.
    """


class InvalidRepoNameError(ValueError):
    """Raised when a subgroup or repository name is not a safe path component."""


def validate_repo_name(kind: str, name: str) -> None:
    """Reject a subgroup or repository name that is not a plain path component.

    Args:
        kind: Which name it is, for the error message.
        name: The name to check.

    Raises:
        InvalidRepoNameError: If the name does not match ``REPO_NAME_PATTERN``.
    """
    if not isinstance(name, str) or not REPO_NAME_PATTERN.fullmatch(name):
        raise InvalidRepoNameError(
            f"Invalid {kind} {name!r}: expected lowercase letters, digits, '.', '_' "
            "or '-', starting with a letter or digit"
        )


def find_case_insensitive(directory: Path, wanted: str, want_dir: bool) -> str | None:
    """Return the name of an entry in ``directory`` that equals ``wanted`` ignoring case.

    GitLab matched paths ignoring case, so some existing names are uppercase.
    An exact-case match wins; among other matches the sorted first is used,
    with a warning. Hidden and non-ASCII names never match.

    Args:
        directory: Directory to search; may be missing.
        wanted: Name to look for.
        want_dir: Accept only directories; otherwise also symlinks.

    Returns:
        The on-disk name, or None if nothing matches.
    """
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return None
    matches = []
    for entry in entries:
        name = entry.name
        if not name.isascii() or name.startswith(".") or name.lower() != wanted.lower():
            continue
        try:
            if entry.is_dir() or (not want_dir and entry.is_symlink()):
                matches.append(name)
        except OSError:
            continue
    if not matches:
        return None
    if wanted in matches:
        return wanted
    matches.sort()
    if len(matches) > 1:
        console.print(
            f"[yellow]Several entries match {wanted!r} in {directory} ignoring case: "
            f"{matches}; using {matches[0]!r}[/yellow]"
        )
    return matches[0]


class BranchAlreadyExistsError(Exception):
    """Raised when a non-forced dump targets an existing branch.

    Attributes:
        branch: The existing branch.
        repo_url: Web URL of that branch.
        repo_path: Repository path, ``dumps/<subgroup>/<repo>``.
    """

    def __init__(self, branch: str, repo_url: str, repo_path: str):
        """Create the error.

        Args:
            branch: The existing branch.
            repo_url: Web URL of that branch.
            repo_path: Repository path, ``dumps/<subgroup>/<repo>``.
        """
        self.branch = branch
        self.repo_url = repo_url
        self.repo_path = repo_path
        super().__init__(f"Branch '{branch}' already exists in {repo_url}")


def repo_web_url(repo_path: str, branch: str) -> str:
    """Build the cgit URL of a branch.

    Args:
        repo_path: Repository path, ``dumps/<subgroup>/<repo>``.
        branch: Branch name.

    Returns:
        The branch's tree URL under ``REPO_PUBLIC_BASE_URL``.
    """
    base = settings.REPO_PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/{repo_path}/tree/?h={quote(branch, safe='')}"


class RepoManager:
    """Commit a dump and push it to its bare repository."""

    def __init__(self, work_dir: str):
        """Create a manager for one dump.

        Args:
            work_dir: Directory holding the extracted dump.
        """
        self.work_dir = Path(work_dir)
        self.repo_root = Path(settings.REPO_ROOT)
        self.org = "dumps"
        self.commit_host = urlparse(settings.REPO_PUBLIC_BASE_URL).hostname or "localhost"

    async def create_and_push_repository(
        self,
        device_props: Dict[str, Any],
        force: bool = False,
    ) -> Tuple[str, str]:
        """Commit the work dir and push it, creating the repository if needed.

        Existing subgroups and repositories are matched ignoring case and their
        on-disk names are used. After the push, HEAD is pointed at the branch,
        the clone indexes are rebuilt and, for a new repository, the cgit repo
        list is refreshed.

        Args:
            device_props: Uses ``repo_subgroup``, ``repo_name``, ``branch`` and
                ``description``.
            force: Replace the branch if it exists.

        Returns:
            ``(repo_url, repo_path)``: the branch's web URL and ``dumps/<subgroup>/<repo>``.

        Raises:
            InvalidRepoNameError: If a name is not a safe path component.
            BranchAlreadyExistsError: If the branch exists (or another worker
                pushed it meanwhile) and ``force`` is False.
            RepoPublishError: If a git or filesystem step fails; the details
                are logged, not included in the message.
        """
        repo_subgroup = device_props["repo_subgroup"]
        repo_name = device_props["repo_name"]
        branch = device_props["branch"]
        description = device_props["description"]

        validate_repo_name("repo_subgroup", repo_subgroup)
        validate_repo_name("repo_name", repo_name)
        org_dir = self.repo_root / self.org
        repo_subgroup = (
            find_case_insensitive(org_dir, repo_subgroup, want_dir=True) or repo_subgroup
        )
        repo_dir = find_case_insensitive(
            org_dir / repo_subgroup, f"{repo_name}.git", want_dir=False
        )
        # The path is rebuilt as "<name>.git", so only an exact ".git" suffix counts.
        if repo_dir and repo_dir.endswith(".git"):
            repo_name = repo_dir[: -len(".git")]

        repo_path = f"{self.org}/{repo_subgroup}/{repo_name}"
        repo_url = repo_web_url(repo_path, branch)
        bare_repo = org_dir / repo_subgroup / f"{repo_name}.git"
        console.print(f"[blue]Target repository: {bare_repo}[/blue]")

        step = "checking the branch"
        try:
            if await self._branch_exists(bare_repo, branch):
                if not force:
                    raise BranchAlreadyExistsError(branch, repo_url, repo_path)
                console.print(f"[yellow]Branch {branch} exists, force-pushing replacement[/yellow]")

            step = "committing the dump"
            await self._setup_git_repository(branch, description)
            step = "creating the repository"
            created = await self._ensure_bare_repo(bare_repo, branch)
            step = "pushing the dump"
            try:
                await self._push(bare_repo, branch, force=force)
            except ProcessException:
                # Another worker may have pushed the same branch since the check
                # above; git then rejects our unrelated history.
                if not force and await self._branch_exists(bare_repo, branch):
                    raise BranchAlreadyExistsError(branch, repo_url, repo_path) from None
                raise
        except (ProcessException, RuntimeError, OSError) as e:
            # Git errors carry the command line and server paths: log them here,
            # and keep them out of the message (and traceback) users see.
            console.print(f"[red]Repository step failed while {step}: {e}[/red]")
            raise RepoPublishError(
                f"Publishing the dump failed while {step}; details are in the worker log."
            ) from None

        await self._set_default_branch(bare_repo, branch)
        await self._rebuild_indexes(bare_repo)
        if created:
            await self._refresh_repolist()

        console.print(f"[green]Successfully pushed to: {repo_url}[/green]")
        return repo_url, repo_path

    async def _branch_exists(self, bare_repo: Path, branch: str) -> bool:
        """Check whether ``refs/heads/<branch>`` exists in the bare repository.

        Args:
            bare_repo: The bare repository; may not exist yet.
            branch: Branch name.

        Returns:
            True if the branch exists; False if it or the repository does not.

        Raises:
            RuntimeError: If git cannot tell (e.g. a broken repository).
        """
        if not bare_repo.exists():
            return False

        result = await run_git_command(
            f"--git-dir={bare_repo}",
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            check=False,
            description="Checking whether branch exists",
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise RuntimeError(
            f"Could not check branch '{branch}' in {bare_repo}: "
            f"git exited {result.returncode}: {result.stderr.strip()}"
        )

    async def _ensure_bare_repo(self, bare_repo: Path, branch: str) -> bool:
        """Create the bare repository if missing, safely against concurrent workers.

        ``git init`` runs in a hidden staging sibling that is renamed into
        place atomically, so the public path never holds a half-initialized
        repository, even if the worker is killed. The loser of the rename
        uses the winner's repository.

        Args:
            bare_repo: Repository path.
            branch: Initial branch.

        Returns:
            True if this call created the repository.

        Raises:
            RuntimeError: If ``bare_repo`` is a dangling symlink.
            ProcessException: If ``git init`` fails.
            OSError: If a directory cannot be created or renamed.
        """
        if bare_repo.exists():
            return False
        if bare_repo.is_symlink():
            raise RuntimeError(f"Repository path is a dangling symlink: {bare_repo}")

        # Default modes on purpose (no explicit mode or mkdtemp), so the new
        # dirs keep the permissions inherited from their parent.
        bare_repo.parent.mkdir(parents=True, exist_ok=True)
        staging = bare_repo.parent / f".{bare_repo.name}.init-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            await run_git_command(
                "init", "--quiet", "--bare", f"--initial-branch={branch}", str(staging),
                description="Creating bare repository",
            )
            try:
                os.rename(staging, bare_repo)
                return True
            except OSError:
                if not bare_repo.exists():
                    raise
                console.print(f"[yellow]{bare_repo} was created concurrently, using it[/yellow]")
                return False
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    async def _setup_git_repository(self, branch: str, description: str) -> None:
        """Commit the work dir as the first commit of a new local repository.

        Args:
            branch: Initial branch name.
            description: Commit message.

        Raises:
            ProcessException: If a git command fails.
        """
        console.print("[blue]Setting up git repository...[/blue]")

        # Initialize git repository
        await run_git_command(
            "init", "--initial-branch", branch,
            cwd=self.work_dir,
            description="Initializing git repository"
        )

        # Configure git user
        await run_git_command(
            "config", "user.name", "dumper",
            cwd=self.work_dir,
            description="Configuring git user name"
        )

        await run_git_command(
            "config", "user.email", f"dumper@{self.commit_host}",
            cwd=self.work_dir,
            description="Configuring git user email"
        )

        # Add all files
        console.print("[blue]Adding files to git...[/blue]")
        await run_git_command(
            "add", "--ignore-errors", "-A",
            cwd=self.work_dir,
            timeout=ONE_HOUR,
            description="Adding files to git"
        )

        # Commit files
        console.print("[blue]Committing files...[/blue]")
        await run_git_command(
            "commit", "--quiet", "--signoff", "--message", description,
            cwd=self.work_dir,
            timeout=HALF_HOUR,
            description="Committing files"
        )

        console.print("[green]Git repository setup completed[/green]")

    async def _push(self, bare_repo: Path, branch: str, force: bool = False) -> None:
        """Push the work dir's HEAD to ``refs/heads/<branch>`` of the bare repository.

        Args:
            bare_repo: Target repository.
            branch: Target branch.
            force: Overwrite the branch.

        Raises:
            ProcessException: If the push fails.
        """
        push_args = ["push", f"--receive-pack={RECEIVE_PACK_COMMAND}"]
        if force:
            push_args.append("--force")
        push_args.extend([str(bare_repo.absolute()), f"HEAD:refs/heads/{branch}"])

        await run_git_command(
            *push_args,
            cwd=self.work_dir,
            timeout=ONE_HOUR,
            description="Pushing to repository"
        )

    async def _set_default_branch(self, bare_repo: Path, branch: str) -> None:
        """Make ``branch`` the default branch shown by cgit; failure only warns.

        Args:
            bare_repo: The bare repository.
            branch: Branch to make the default.
        """
        result = await run_git_command(
            f"--git-dir={bare_repo}",
            "symbolic-ref", "HEAD", f"refs/heads/{branch}",
            check=False,
            description=f"Setting default branch to {branch}",
        )
        if not result.success:
            console.print(f"[yellow]Failed to set default branch: {result.stderr.strip()}[/yellow]")

    async def _rebuild_indexes(self, bare_repo: Path) -> None:
        """Rebuild the multi-pack index and bitmap used to serve clones; failure only warns.

        Args:
            bare_repo: The bare repository.
        """
        result = await run_command(
            settings.REPO_INDEX_GIT_BINARY,
            f"--git-dir={bare_repo}",
            "multi-pack-index", "write", "--bitmap",
            timeout=ONE_HOUR,
            description="Rebuilding multi-pack index",
        )
        if not result.success:
            console.print(
                f"[yellow]Warning: multi-pack-index rebuild failed: {result.stderr.strip()}[/yellow]"
            )

    async def _refresh_repolist(self) -> None:
        """Run ``CGIT_REPOLIST_COMMAND``, if set, so a new repository is listed; failure only warns."""
        command = shlex.split(settings.CGIT_REPOLIST_COMMAND)
        if not command:
            return
        result = await run_command(
            *command,
            timeout=FIVE_MINUTES,
            description="Refreshing cgit repository list",
        )
        if not result.success:
            console.print(
                f"[yellow]Warning: cgit repo list refresh failed: {result.stderr.strip()}[/yellow]"
            )

    async def send_channel_notification(
        self,
        device_props: Dict[str, Any],
        repo_url: str,
        download_url: str,
        is_whitelisted: bool,
        add_blacklist: bool,
        api_key: str,
    ) -> None:
        """Announce a published dump in the Telegram channel.

        Args:
            device_props: Device properties for the message.
            repo_url: Web URL of the pushed branch.
            download_url: Firmware URL, shown only if whitelisted.
            is_whitelisted: Whether the firmware URL is whitelisted.
            add_blacklist: Hide the firmware URL.
            api_key: Telegram bot token.

        Raises:
            httpx.HTTPError: If the Telegram API cannot be reached.
        """
        console.print("[blue]Sending channel notification...[/blue]")
        include_download_url = is_whitelisted and not add_blacklist
        if include_download_url:
            console.print("[blue]Including firmware link in channel notification[/blue]")
        else:
            console.print("[blue]Sending channel notification without firmware link[/blue]")

        # Build notification message using utility function
        message = format_channel_notification_message(
            device_props, repo_url, download_url if include_download_url else None
        )

        # Send to channel via Telegram API (use custom base URL if configured)
        api_base = settings.TELEGRAM_API_BASE_URL
        if api_base:
            api_base = api_base.rstrip("/")
        else:
            api_base = "https://api.telegram.org"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_base}/bot{api_key}/sendMessage",
                data={
                    "text": message,
                    "chat_id": "@android_dumps",
                    "parse_mode": settings.DEFAULT_PARSE_MODE,
                    "disable_web_page_preview": True,
                },
                timeout=30.0,
            )

            if response.status_code == 200:
                console.print("[green]Channel notification sent successfully[/green]")
            else:
                console.print(f"[yellow]Failed to send channel notification: {response.text}[/yellow]")

    async def check_whitelist(self, url: str) -> bool:
        """Check whether a URL's host is a whitelisted domain or a subdomain of one.

        Args:
            url: Firmware URL.

        Returns:
            True if whitelisted; False otherwise or if the whitelist is unreadable.
        """
        whitelist_file = Path.home() / "dumpbot" / "whitelist.txt"

        if not whitelist_file.exists():
            console.print("[yellow]Whitelist file not found[/yellow]")
            return False

        try:
            with open(whitelist_file, 'r') as f:
                whitelist_domains = [line.strip() for line in f if line.strip()]

            hostname = (urlparse(url).hostname or "").lower()

            for domain in whitelist_domains:
                normalized_domain = domain.lower()
                if hostname == normalized_domain or hostname.endswith(f".{normalized_domain}"):
                    console.print(f"[green]URL is whitelisted (domain: {domain})[/green]")
                    return True

            console.print("[yellow]URL is not whitelisted[/yellow]")
            return False

        except Exception as e:
            console.print(f"[red]Error checking whitelist: {e}[/red]")
            return False
