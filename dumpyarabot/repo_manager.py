"""Publish dumps into bare git repositories served by cgit.

Repositories live at ``<REPO_ROOT>/dumps/<subgroup>/<repo>.git``. Existing
ones may be symlinks to other storage and owned by another user.

A dump is not pushed: parallel ``git fast-import`` processes write it as
packs into a private object store, and those packs are then dropped straight
into the target's ``objects/pack`` before the branch is updated. Every
command on a repository gets an explicit ``--git-dir``, so git skips its
ownership (safe.directory) check.
"""

import asyncio
import functools
import heapq
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Tuple
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
    run_command_with_input,
    run_git_command,
)

console = Console()

# Names come from firmware properties, so they are untrusted: each must be a
# single plain path component (no "/", ".", ".." or hidden names).
REPO_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Ref updates run the target's reference-transaction hook; repos may be owned
# by someone else, so their hooks must not run as the bot.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")

# Large files are streamed to fast-import in pieces of this size.
READ_CHUNK = 1 << 20

# Pack files in the order they are dropped into the target: git finds a pack
# through its .idx, so that comes last and readers never see an incomplete one.
PACK_SUFFIXES = (".pack", ".rev", ".idx")


@dataclass
class DumpFile:
    """A file or symlink of the work dir to import as a blob.

    Attributes:
        path: Path relative to the work dir, as git records it.
        source: Absolute path on disk.
        mode: Git file mode: ``100644``, ``100755`` or ``120000``.
        size: Size in bytes, used to balance the import processes.
        link_target: For a symlink, its target text (the blob's content).
    """

    path: bytes
    source: bytes
    mode: str
    size: int
    link_target: bytes | None = None


def collect_files(work_dir: Path, skip: Path) -> list[DumpFile]:
    """List the files and symlinks of a dump the way ``git add -A`` would see them.

    Any path component named ``.git`` (ignoring case) is left out, as git
    does, and so are empty directories and anything that is not a regular
    file or symlink. Symlinks are never followed. ``.gitignore`` files are
    not honoured: everything is published.

    Args:
        work_dir: The dump's directory.
        skip: A directory directly inside ``work_dir`` to leave out.

    Returns:
        The files found; unreadable directories are skipped with a warning.
    """
    files = []
    skip_path = os.fsencode(skip)
    pending = [(os.fsencode(work_dir), b"")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as e:
            console.print(f"[yellow]Skipping unreadable directory {e.filename!r}: {e.strerror}[/yellow]")
            continue
        for entry in entries:
            if entry.name.lower() == b".git" or entry.path == skip_path:
                continue
            path = prefix + entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(entry.path)
                files.append(DumpFile(path, entry.path, "120000", len(target), target))
            elif stat.S_ISDIR(st.st_mode):
                pending.append((entry.path, path + b"/"))
            elif stat.S_ISREG(st.st_mode):
                # Like git, only the owner's execute bit counts.
                mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                files.append(DumpFile(path, entry.path, mode, st.st_size))
    return files


def split_by_size(
    blobs: list[tuple[int, DumpFile]], count: int
) -> list[list[tuple[int, DumpFile]]]:
    """Split ``(mark, file)`` pairs into ``count`` chunks of about equal total size.

    Largest files go first, each to the currently lightest chunk.

    Args:
        blobs: Pairs to split.
        count: Number of chunks; at least 1.

    Returns:
        The chunks; some are empty if there are fewer files than chunks.
    """
    chunks: list[list[tuple[int, DumpFile]]] = [[] for _ in range(count)]
    loads = [(0, i) for i in range(count)]
    for blob in sorted(blobs, key=lambda b: b[1].size, reverse=True):
        load, i = heapq.heappop(loads)
        chunks[i].append(blob)
        heapq.heappush(loads, (load + blob[1].size, i))
    return chunks


def feed_bytes(data: bytes) -> Callable[[asyncio.StreamWriter], Awaitable[None]]:
    """Make a stdin feeder for ``run_command_with_input`` that writes fixed data.

    Args:
        data: What to write.

    Returns:
        The feeder.
    """

    async def feed(stdin: asyncio.StreamWriter) -> None:
        stdin.write(data)
        await stdin.drain()

    return feed


async def feed_blobs(stdin: asyncio.StreamWriter, blobs: list[tuple[int, DumpFile]]) -> None:
    """Stream ``blob`` commands for fast-import, reading files in pieces.

    A file that cannot be opened is skipped with a warning, so it gets no
    mark and is left out of the commit (like ``git add --ignore-errors``).

    Args:
        stdin: fast-import's standard input.
        blobs: ``(mark, file)`` pairs to import.

    Raises:
        OSError: If a file cannot be read or shrinks while being read.
    """
    for mark, file in blobs:
        if file.link_target is not None:
            stdin.write(b"blob\nmark :%d\ndata %d\n%s\n" % (mark, file.size, file.link_target))
            await stdin.drain()
            continue
        try:
            handle = await asyncio.to_thread(open, file.source, "rb")
        except OSError as e:
            console.print(f"[yellow]Skipping unreadable file {file.path!r}: {e.strerror}[/yellow]")
            continue
        with handle:
            remaining = os.fstat(handle.fileno()).st_size
            stdin.write(b"blob\nmark :%d\ndata %d\n" % (mark, remaining))
            while remaining:
                data = await asyncio.to_thread(handle.read, min(READ_CHUNK, remaining))
                if not data:
                    raise OSError(f"{file.path!r} shrank while being imported")
                stdin.write(data)
                remaining -= len(data)
                await stdin.drain()
        stdin.write(b"\n")


def same_filesystem(a: Path, b: Path) -> bool:
    """Tell whether two existing paths are on the same filesystem.

    Args:
        a: First path.
        b: Second path.

    Returns:
        True if both are on the same device, so files can be hard-linked.
    """
    return os.stat(a).st_dev == os.stat(b).st_dev


def drop_packs(source: Path, target: Path) -> int:
    """Hard-link or copy every pack of ``source`` into the pack dir ``target``.

    All ``.pack`` files go in first, then ``.rev``, then ``.idx``. Packs are
    hard-linked on the same filesystem; otherwise each file is copied to a
    ``tmp_`` name (which ``git gc`` cleans up if left behind), synced and
    renamed. A pack whose name already exists in ``target`` is skipped, since
    names are content hashes. Nothing is ever removed again: another worker
    may already rely on a pack with the same name, and an unreferenced pack
    is harmless garbage.

    Args:
        source: The private store's ``objects/pack`` dir.
        target: The target repository's (resolved) ``objects/pack`` dir.

    Returns:
        The number of packs added to ``target``.

    Raises:
        OSError: If a file cannot be linked or copied.
    """
    link = same_filesystem(source, target)
    names = [p.stem for p in sorted(source.glob("pack-*.pack"))]
    names = [n for n in names if not (target / f"{n}.pack").exists()]
    for suffix in PACK_SUFFIXES:
        for name in names:
            src, dst = source / f"{name}{suffix}", target / f"{name}{suffix}"
            if not src.exists():
                continue
            if link:
                os.link(src, dst)
            else:
                tmp = target / f"tmp_{secrets.token_hex(8)}{suffix}"
                with open(src, "rb") as fin, open(tmp, "xb") as fout:
                    shutil.copyfileobj(fin, fout, READ_CHUNK)
                    fout.flush()
                    os.fsync(fout.fileno())
                os.rename(tmp, dst)
    return len(names)


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
    """Commit a dump and publish it into its bare repository."""

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
        """Commit the work dir and publish it, creating the repository if needed.

        Existing subgroups and repositories are matched ignoring case and their
        on-disk names are used. The dump is imported into a private object
        store inside the work dir, its packs are dropped into the repository
        and the branch is updated atomically. Then HEAD is pointed at the
        branch, the clone indexes are rebuilt and, for a new repository, the
        cgit repo list is refreshed.

        Args:
            device_props: Uses ``repo_subgroup``, ``repo_name``, ``branch`` and
                ``description``.
            force: Replace the branch if it exists.

        Returns:
            ``(repo_url, repo_path)``: the branch's web URL and ``dumps/<subgroup>/<repo>``.

        Raises:
            InvalidRepoNameError: If a name is not a safe path component.
            BranchAlreadyExistsError: If the branch exists (or another worker
                published it meanwhile) and ``force`` is False.
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
        store = None
        try:
            if await self._branch_tip(bare_repo, branch):
                if not force:
                    raise BranchAlreadyExistsError(branch, repo_url, repo_path)
                console.print(f"[yellow]Branch {branch} exists, replacing it[/yellow]")

            step = "importing the dump"
            store = Path(tempfile.mkdtemp(prefix=".publish-", dir=self.work_dir))
            commit = await self._import(store, description)
            step = "creating the repository"
            created = await self._ensure_bare_repo(bare_repo, branch)
            step = "publishing the dump"
            try:
                await self._publish(store, bare_repo, branch, commit, force)
            except ProcessException:
                # Another worker may have published the same branch since the
                # check above.
                if not force and await self._branch_tip(bare_repo, branch):
                    raise BranchAlreadyExistsError(branch, repo_url, repo_path) from None
                raise
        except (ProcessException, RuntimeError, OSError) as e:
            # Git errors carry the command line and server paths: log them here,
            # and keep them out of the message (and traceback) users see.
            console.print(f"[red]Repository step failed while {step}: {e}[/red]")
            raise RepoPublishError(
                f"Publishing the dump failed while {step}; details are in the worker log."
            ) from None
        finally:
            if store is not None:
                shutil.rmtree(store, ignore_errors=True)

        await self._set_default_branch(bare_repo, branch)
        await self._rebuild_indexes(bare_repo)
        if created:
            await self._refresh_repolist()

        console.print(f"[green]Successfully published to: {repo_url}[/green]")
        return repo_url, repo_path

    async def _import(self, store: Path, description: str) -> str:
        """Write the work dir as a commit into a new private object store.

        Files are split into ``REPO_IMPORT_JOBS`` size-balanced chunks, each
        imported by its own ``git fast-import`` straight into a pack.
        ``fastimport.unpackLimit=0`` keeps even small imports as packs, and
        ``core.bigFileThreshold=1`` skips delta search. The tree and commit
        are then built from the marks and packed too, and the store is
        checked to hold everything the commit needs, in packs only.

        Args:
            store: An empty directory on the work dir's filesystem.
            description: Commit message.

        Returns:
            The commit's object id.

        Raises:
            ProcessException: If a git command fails, including the final
                fsck when the store is missing objects.
            OSError: If a file cannot be read.
        """
        git_dir = f"--git-dir={store}"
        await run_git_command("init", "--quiet", "--bare", str(store), description="Creating object store")

        files = await asyncio.to_thread(collect_files, self.work_dir, store)
        jobs = max(1, min(settings.REPO_IMPORT_JOBS, len(files)))
        console.print(f"[blue]Importing {len(files)} files with {jobs} processes...[/blue]")
        marks = [store / f"marks-{i}" for i in range(jobs)]
        numbered = list(enumerate(files, start=1))
        await asyncio.gather(*(
            run_command_with_input(
                "git", "-c", "core.bigFileThreshold=1", "-c", "fastimport.unpackLimit=0",
                git_dir, "fast-import", "--quiet", f"--export-marks={marks[i]}",
                feed=functools.partial(feed_blobs, blobs=chunk),
                timeout=ONE_HOUR,
                description=f"Importing files ({i + 1}/{jobs})",
            )
            for i, chunk in enumerate(split_by_size(numbered, jobs))
        ))

        object_ids = {}
        for path in marks:
            if path.exists():
                for line in path.read_text().splitlines():
                    mark, oid = line.split()
                    object_ids[int(mark[1:])] = oid
        index_info = b"".join(
            b"%s %s\t%s\0" % (f.mode.encode(), object_ids[k].encode(), f.path)
            for k, f in numbered if k in object_ids
        )
        index_env = {"GIT_INDEX_FILE": str(store / "index")}
        await run_command_with_input(
            "git", git_dir, "update-index", "-z", "--index-info",
            feed=feed_bytes(index_info), env=index_env, timeout=HALF_HOUR, description="Building the index",
        )
        tree = (await run_command(
            "git", git_dir, "write-tree",
            env=index_env, check=True, timeout=HALF_HOUR, description="Writing the tree",
        )).stdout.strip()
        identity = f"dumper <dumper@{self.commit_host}>"
        commit = (await run_git_command(
            "-c", "user.name=dumper", "-c", f"user.email=dumper@{self.commit_host}",
            git_dir, "commit-tree", tree,
            "-m", description, "-m", f"Signed-off-by: {identity}",
            description="Committing the dump",
        )).stdout.strip()

        # Pack every reachable object not yet in a pack (the trees and the
        # commit), then drop the loose copies, so the fsck below proves that
        # the packs alone hold the whole commit.
        await run_command_with_input(
            "git", git_dir, "pack-objects", "--revs", "--unpacked", "--window=0", "--quiet",
            str(store / "objects" / "pack" / "pack"),
            feed=feed_bytes(f"{commit}\n".encode()), timeout=HALF_HOUR, description="Packing trees and commit",
        )
        await run_git_command(git_dir, "prune-packed", description="Removing packed loose objects")
        await run_git_command(
            git_dir, "fsck", "--connectivity-only", "--no-dangling", "--no-progress", commit,
            timeout=HALF_HOUR, description="Checking the imported dump",
        )
        return commit

    async def _publish(
        self, store: Path, bare_repo: Path, branch: str, commit: str, force: bool
    ) -> None:
        """Drop the store's packs into the repository and point the branch at ``commit``.

        The branch is updated with an expected old value, so a concurrent
        update makes this fail instead of being overwritten: without
        ``force`` the branch must not exist; with it, it must still have the
        value read just before. If the update fails, the dropped packs stay
        behind unreferenced (see ``drop_packs``).

        Args:
            store: The private object store holding the commit.
            bare_repo: Target repository; may be a symlink to other storage.
            branch: Target branch.
            commit: Commit to publish.
            force: Replace the branch if it exists.

        Raises:
            ProcessException: If the ref update fails, e.g. because the branch
                exists or changed meanwhile.
            OSError: If the packs cannot be dropped.
        """
        target = bare_repo.resolve() / "objects" / "pack"
        added = await asyncio.to_thread(drop_packs, store / "objects" / "pack", target)
        old = await self._branch_tip(bare_repo, branch) if force else ""
        try:
            await run_git_command(
                *NO_HOOKS, f"--git-dir={bare_repo}",
                "update-ref", f"refs/heads/{branch}", commit, old,
                description="Updating the branch",
            )
        except ProcessException:
            console.print(f"[yellow]Branch update failed; leaving {added} unreferenced pack(s)[/yellow]")
            raise

    async def _branch_tip(self, bare_repo: Path, branch: str) -> str:
        """Read the commit ``refs/heads/<branch>`` points at.

        Args:
            bare_repo: The bare repository; may not exist yet.
            branch: Branch name.

        Returns:
            The commit id, or "" if the branch or the repository does not exist.

        Raises:
            RuntimeError: If git cannot tell (e.g. a broken repository).
        """
        if not bare_repo.exists():
            return ""

        result = await run_git_command(
            f"--git-dir={bare_repo}",
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            check=False,
            description="Checking whether branch exists",
        )
        if result.returncode in (0, 1):
            return result.stdout.strip()
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

    async def _set_default_branch(self, bare_repo: Path, branch: str) -> None:
        """Make ``branch`` the default branch shown by cgit; failure only warns.

        Args:
            bare_repo: The bare repository.
            branch: Branch to make the default.
        """
        result = await run_git_command(
            *NO_HOOKS, f"--git-dir={bare_repo}",
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
        result = await run_git_command(
            f"--git-dir={bare_repo}",
            "multi-pack-index", "write", "--bitmap",
            check=False,
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
