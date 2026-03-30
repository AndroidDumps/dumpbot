import secrets
from typing import List, Tuple

import httpx
from rich.console import Console

from dumpyarabot import schemas
from dumpyarabot.config import settings

console = Console()


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return secrets.token_hex(4)  # 8-character hex string


async def get_jenkins_builds(job_name: str) -> List[schemas.JenkinsBuild]:
    """Fetch all builds from Jenkins for a specific job."""
    console.print(f"[blue]Fetching builds for job: {job_name}[/blue]")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{settings.JENKINS_URL}/job/{job_name}/api/json",
                params={
                    "tree": "allBuilds[number,result,actions[parameters[name,value]]]"
                },
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                timeout=30.0,
            )
            response.raise_for_status()
            builds = [
                schemas.JenkinsBuild(**build) for build in response.json()["allBuilds"]
            ]
            console.print(
                f"[green]Successfully fetched {len(builds)} builds for {job_name}[/green]"
            )
            return builds
        except Exception as e:
            console.print(f"[red]Failed to fetch builds for {job_name}: {e}[/red]")
            raise


def _is_matching_build(
    build: schemas.JenkinsBuild, args: schemas.DumpArguments
) -> bool:
    """Check if a build matches the given arguments."""
    for action in build.actions:
        if "parameters" in action:
            params = {param["name"]: param["value"] for param in action["parameters"]}
            if matches := (
                params.get("URL") == args.url.unicode_string()
                and params.get("USE_ALT_DUMPER") == args.use_alt_dumper
            ):
                console.print("[green]Found matching build parameters[/green]")
                console.print(f"[blue]Build params: {params}[/blue]")
                console.print(
                    f"[blue]Looking for: URL={args.url.unicode_string()}, ALT={args.use_alt_dumper}, PRIVDUMP={args.use_privdump}[/blue]"
                )
                return matches
    return False


def _get_build_status(build: schemas.JenkinsBuild) -> Tuple[bool, str]:
    """Get the status of a build."""
    console.print(f"[blue]Checking build status: #{build.number}[/blue]")
    if build.result is None:
        console.print("[yellow]Build is currently in progress[/yellow]")
        return (
            True,
            f"Build #{build.number} is currently in progress for this URL and settings.",
        )
    elif build.result == "SUCCESS":
        console.print("[green]Build completed successfully[/green]")
        return (
            True,
            f"Build #{build.number} has already successfully completed for this URL and settings.",
        )
    else:
        console.print(
            f"[yellow]Build result was {build.result}, will start new build[/yellow]"
        )
        return (
            False,
            f"Build #{build.number} exists for this URL and settings, but result was {build.result}. A new build will be started.",
        )


async def check_existing_build(args: schemas.DumpArguments) -> Tuple[bool, str]:
    """Check if a build with the given parameters already exists."""
    job_name = "privdump" if args.use_privdump else "dumpyara"
    console.print(f"[blue]Checking existing builds for {job_name}[/blue]")
    console.print("Build parameters:", args)

    builds = await get_jenkins_builds(job_name)

    for build in builds:
        if _is_matching_build(build, args):
            status = _get_build_status(build)
            console.print(f"[yellow]Found matching build - Status: {status}[/yellow]")
            return status

    console.print(f"[green]No matching build found for {job_name}[/green]")
    return False, f"No matching build found. A new {job_name} build will be started."


async def call_jenkins(args: schemas.DumpArguments, add_blacklist: bool = False) -> str:
    """Call Jenkins to start a new build."""
    job_name = "privdump" if args.use_privdump else "dumpyara"
    console.print(f"[blue]Starting new {job_name} build[/blue]")

    # Prepare Jenkins parameters
    jenkins_params = {
        "URL": args.url.unicode_string(),
        "USE_ALT_DUMPER": args.use_alt_dumper,
        "ADD_BLACKLIST": add_blacklist,
        "INITIAL_MESSAGE_ID": args.initial_message_id,
        "INITIAL_CHAT_ID": args.initial_chat_id,
    }

    jenkins_url = f"{settings.JENKINS_URL}/job/{job_name}/buildWithParameters"

    # Debug: Show replicable Jenkins command
    console.print(f"[yellow]=== JENKINS DEBUG COMMAND ===[/yellow]")
    console.print(f"[cyan]Job: {job_name}[/cyan]")
    console.print(f"[cyan]URL: {jenkins_url}[/cyan]")
    console.print(f"[cyan]Parameters:[/cyan]")
    for key, value in jenkins_params.items():
        console.print(f"  {key} = {value} ({type(value).__name__})")

    # Create curl command for replication (matches httpx params behavior - URL query parameters)
    param_string = "&".join([f"{key}={value}" for key, value in jenkins_params.items()])
    curl_command = f'curl -X POST "{jenkins_url}?{param_string}" \\\n'
    curl_command += f'  -u "{settings.JENKINS_USER_NAME}:***"'

    console.print(f"[green]Equivalent curl command:[/green]")
    console.print(f"[dim]{curl_command}[/dim]")
    console.print(f"[yellow]=== END JENKINS DEBUG ===[/yellow]")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                jenkins_url,
                params=jenkins_params,
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
            )
            response.raise_for_status()
            console.print(f"[green]Successfully triggered {job_name} build[/green]")
            console.print(f"[blue]Response headers: {dict(response.headers)}[/blue]")

            # Try to get queue item ID from Location header for tracking
            queue_item_id = None
            if "Location" in response.headers:
                location = response.headers["Location"]
                console.print(f"[blue]Build queue location: {location}[/blue]")
                # Extract queue item ID from location URL
                if "/queue/item/" in location:
                    queue_item_id = location.split("/queue/item/")[1].rstrip("/")
                    console.print(f"[blue]Queue item ID: {queue_item_id}[/blue]")

            if queue_item_id:
                return f"{job_name.capitalize()} job triggered (Queue ID: {queue_item_id})"
            else:
                return f"{job_name.capitalize()} job triggered"
        except Exception as e:
            console.print(f"[red]Failed to trigger {job_name} build: {e}[/red]")
            raise


async def cancel_jenkins_job(job_id: str, use_privdump: bool = False) -> str:
    """Cancel a Jenkins job."""
    job_name = "privdump" if use_privdump else "dumpyara"
    console.print(f"[blue]Attempting to cancel {job_name} job {job_id}[/blue]")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{settings.JENKINS_URL}/job/{job_name}/{job_id}/stop",
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                follow_redirects=True,
            )
            if response.status_code == 200:
                console.print(
                    f"[green]Successfully cancelled {job_name} job {job_id}[/green]"
                )
                return f"Job with ID {job_id} has been cancelled in {job_name}."
            elif response.status_code == 404:
                console.print(
                    f"[yellow]Job {job_id} not found, checking queue[/yellow]"
                )
                response = await client.post(
                    f"{settings.JENKINS_URL}/queue/cancelItem",
                    params={"id": job_id},
                    auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                    follow_redirects=True,
                )
                if response.status_code == 204:
                    console.print(
                        f"[green]Successfully removed {job_name} job {job_id} from queue[/green]"
                    )
                    return f"Job with ID {job_id} has been removed from the {job_name} queue."

            console.print(f"[yellow]Failed to cancel {job_name} job {job_id}[/yellow]")
            return f"Failed to cancel job with ID {job_id} in {job_name}. Job not found or already completed."
        except Exception as e:
            console.print(
                f"[red]Error while cancelling {job_name} job {job_id}: {e}[/red]"
            )
            raise
