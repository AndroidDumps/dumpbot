from typing import List, Tuple

import httpx
from rich.console import Console

from dumpyarabot import schemas
from dumpyarabot.config import settings

console = Console()


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
    console.print("[blue]Checking build parameters match...[/blue]")
    for action in build.actions:
        if "parameters" in action:
            params = {param["name"]: param["value"] for param in action["parameters"]}
            matches = (
                params.get("URL") == args.url.unicode_string()
                and params.get("USE_ALT_DUMPER") == args.use_alt_dumper
                and params.get("ADD_BLACKLIST") == args.add_blacklist
            )
            if matches:
                console.print("[green]Found matching build parameters[/green]")
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


async def call_jenkins(args: schemas.DumpArguments) -> str:
    """Call Jenkins to start a new build."""
    job_name = "privdump" if args.use_privdump else "dumpyara"
    console.print(f"[blue]Starting new {job_name} build[/blue]")
    console.print("Build parameters:", args)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{settings.JENKINS_URL}/job/{job_name}/buildWithParameters",
                params={
                    "URL": args.url.unicode_string(),
                    "USE_ALT_DUMPER": args.use_alt_dumper,
                    "ADD_BLACKLIST": args.add_blacklist,
                },
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
            )
            response.raise_for_status()
            console.print(f"[green]Successfully started {job_name} build[/green]")
            return f"{job_name.capitalize()} job started"
        except Exception as e:
            console.print(f"[red]Failed to start {job_name} build: {e}[/red]")
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
