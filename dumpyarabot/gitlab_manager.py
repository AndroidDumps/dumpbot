from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import httpx
from rich.console import Console

from dumpyarabot.config import settings
from dumpyarabot.utils import escape_markdown
from dumpyarabot.process_utils import run_git_command
from dumpyarabot.message_formatting import format_channel_notification_message

console = Console()


class GitLabManager:
    """Handles GitLab repository creation, branch management, and git operations."""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.gitlab_server = "dumps.tadiphone.dev"
        self.push_host = "dumps"
        self.org = "dumps"
        self.parent_group_id = 64

    async def create_and_push_repository(
        self,
        device_props: Dict[str, Any],
        dumper_token: str
    ) -> Tuple[str, str]:
        """Create GitLab repository and push firmware files."""
        repo_subgroup = device_props["repo_subgroup"]
        repo_name = device_props["repo_name"]
        branch = device_props["branch"]
        description = device_props["description"]

        console.print(f"[blue]Creating GitLab repository: {self.org}/{repo_subgroup}/{repo_name}[/blue]")

        # Ensure subgroup exists
        group_id = await self._ensure_subgroup_exists(repo_subgroup, dumper_token)

        # Ensure project exists
        project_id = await self._ensure_project_exists(group_id, repo_name, dumper_token, repo_subgroup)

        # Check if branch already exists
        if await self._branch_exists(project_id, branch, dumper_token):
            repo_url = f"https://{self.gitlab_server}/{self.org}/{repo_subgroup}/{repo_name}/tree/{branch}/"
            raise Exception(f"Branch '{branch}' already exists in {repo_url}")

        # Setup git repository
        await self._setup_git_repository(branch, description)

        # Push to GitLab
        repo_url = await self._push_to_gitlab(project_id, repo_subgroup, repo_name, branch, dumper_token)

        console.print(f"[green]Successfully created and pushed to: {repo_url}[/green]")
        return repo_url, f"{self.org}/{repo_subgroup}/{repo_name}"

    async def _ensure_subgroup_exists(self, subgroup_name: str, dumper_token: str) -> int:
        """Ensure GitLab subgroup exists, create if necessary."""
        console.print(f"[blue]Checking subgroup: {subgroup_name}[/blue]")

        async with httpx.AsyncClient(verify=False) as client:
            # Check if subgroup exists
            response = await client.get(
                f"https://{self.gitlab_server}/api/v4/groups/{self.org}%2f{subgroup_name}",
                headers={"Authorization": f"Bearer {dumper_token}"},
                timeout=30.0
            )

            if response.status_code == 200:
                group_data = response.json()
                group_id = group_data["id"]
                console.print(f"[green]Subgroup {subgroup_name} exists with ID: {group_id}[/green]")
                return group_id

            # Create subgroup
            console.print(f"[blue]Creating subgroup: {subgroup_name}[/blue]")
            create_response = await client.post(
                f"https://{self.gitlab_server}/api/v4/groups",
                headers={"Authorization": f"Bearer {dumper_token}"},
                data={
                    "name": subgroup_name.capitalize(),
                    "parent_id": self.parent_group_id,
                    "path": subgroup_name,
                    "visibility": "public"
                },
                timeout=30.0
            )

            if create_response.status_code in [200, 201]:
                group_data = create_response.json()
                group_id = group_data["id"]
                console.print(f"[green]Created subgroup {subgroup_name} with ID: {group_id}[/green]")
                return group_id
            else:
                raise Exception(f"Failed to create subgroup {subgroup_name}: {create_response.text}")

    async def _ensure_project_exists(self, group_id: int, repo_name: str, dumper_token: str, repo_subgroup: str) -> int:
        """Ensure GitLab project exists, create if necessary."""
        console.print(f"[blue]Checking project: {repo_name}[/blue]")

        async with httpx.AsyncClient(verify=False) as client:
            # Check if project exists (using full path)
            response = await client.get(
                f"https://{self.gitlab_server}/api/v4/projects/{self.org}%2f{repo_subgroup}%2f{repo_name}",
                headers={"Authorization": f"Bearer {dumper_token}"},
                timeout=30.0
            )

            if response.status_code == 200:
                project_data = response.json()
                project_id = project_data["id"]
                console.print(f"[green]Project {repo_name} exists with ID: {project_id}[/green]")
                return project_id

            # Create project
            console.print(f"[blue]Creating project: {repo_name}[/blue]")
            create_response = await client.post(
                f"https://{self.gitlab_server}/api/v4/projects",
                headers={"Authorization": f"Bearer {dumper_token}"},
                data={
                    "namespace_id": group_id,
                    "name": repo_name,
                    "visibility": "public"
                },
                timeout=30.0
            )

            if create_response.status_code in [200, 201]:
                project_data = create_response.json()
                project_id = project_data["id"]
                console.print(f"[green]Created project {repo_name} with ID: {project_id}[/green]")
                return project_id
            else:
                raise Exception(f"Failed to create project {repo_name}: {create_response.text}")

    async def _branch_exists(self, project_id: int, branch: str, dumper_token: str) -> bool:
        """Check if branch already exists in project."""
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(
                f"https://{self.gitlab_server}/api/v4/projects/{project_id}/repository/branches/{branch}",
                headers={"Authorization": f"Bearer {dumper_token}"},
                timeout=30.0
            )

            if response.status_code == 200:
                branch_data = response.json()
                return branch_data.get("name") == branch

            return False

    async def _setup_git_repository(self, branch: str, description: str) -> None:
        """Initialize git repository and configure it."""
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
            "config", "user.email", f"dumper@{self.gitlab_server}",
            cwd=self.work_dir,
            description="Configuring git user email"
        )

        # Add all files
        console.print("[blue]Adding files to git...[/blue]")
        await run_git_command(
            "add", "--ignore-errors", "-A",
            cwd=self.work_dir,
            description="Adding files to git"
        )

        # Commit files
        console.print("[blue]Committing files...[/blue]")
        await run_git_command(
            "commit", "--quiet", "--signoff", "--message", description,
            cwd=self.work_dir,
            description="Committing files"
        )

        console.print("[green]Git repository setup completed[/green]")

    async def _push_to_gitlab(
        self,
        project_id: int,
        repo_subgroup: str,
        repo_name: str,
        branch: str,
        dumper_token: str
    ) -> str:
        """Push git repository to GitLab."""
        console.print("[blue]Pushing to GitLab...[/blue]")

        repo_url = f"{self.push_host}:{self.org}/{repo_subgroup}/{repo_name}.git"
        branch_ref = f"refs/heads/{branch}"

        await run_git_command(
            "push", repo_url, f"HEAD:{branch_ref}",
            cwd=self.work_dir,
            timeout=120.0,
            description="Pushing to GitLab"
        )

        # Set default branch
        await self._set_default_branch(project_id, branch, dumper_token)

        # Generate repository URL
        repo_url = f"https://{self.gitlab_server}/{self.org}/{repo_subgroup}/{repo_name}/tree/{branch}/"
        return repo_url

    async def _set_default_branch(self, project_id: int, branch: str, dumper_token: str) -> None:
        """Set the default branch for the project."""
        console.print(f"[blue]Setting default branch to: {branch}[/blue]")

        async with httpx.AsyncClient(verify=False) as client:
            response = await client.put(
                f"https://{self.gitlab_server}/api/v4/projects/{project_id}",
                headers={"Authorization": f"Bearer {dumper_token}"},
                data={"default_branch": branch},
                timeout=30.0
            )

            if response.status_code == 200:
                console.print(f"[green]Set default branch to: {branch}[/green]")
            else:
                console.print(f"[yellow]Failed to set default branch: {response.text}[/yellow]")

    async def send_channel_notification(
        self,
        device_props: Dict[str, Any],
        repo_url: str,
        download_url: str,
        is_whitelisted: bool,
        add_blacklist: bool,
        api_key: str
    ) -> None:
        """Send notification to Telegram channel."""
        if is_whitelisted and not add_blacklist:
            console.print("[blue]Skipping channel notification (whitelisted and not blacklisted)[/blue]")
            return

        console.print("[blue]Sending channel notification...[/blue]")

        # Build notification message using utility function
        message = format_channel_notification_message(
            device_props, repo_url, download_url
        )

        # Send to channel
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{api_key}/sendMessage",
                data={
                    "text": message,
                    "chat_id": "@android_dumps",
                    "parse_mode": settings.DEFAULT_PARSE_MODE,
                    "disable_web_page_preview": True
                },
                timeout=30.0
            )

            if response.status_code == 200:
                console.print("[green]Channel notification sent successfully[/green]")
            else:
                console.print(f"[yellow]Failed to send channel notification: {response.text}[/yellow]")

    async def check_whitelist(self, url: str) -> bool:
        """Check if URL is in whitelist."""
        whitelist_file = Path.home() / "dumpbot" / "whitelist.txt"

        if not whitelist_file.exists():
            console.print("[yellow]Whitelist file not found[/yellow]")
            return False

        try:
            with open(whitelist_file, 'r') as f:
                whitelist_domains = [line.strip() for line in f if line.strip()]

            for domain in whitelist_domains:
                if domain in url:
                    console.print(f"[green]URL is whitelisted (domain: {domain})[/green]")
                    return True

            console.print("[yellow]URL is not whitelisted[/yellow]")
            return False

        except Exception as e:
            console.print(f"[red]Error checking whitelist: {e}[/red]")
            return False