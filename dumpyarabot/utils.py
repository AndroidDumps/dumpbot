from typing import List, Tuple

import httpx

from dumpyarabot import schemas
from dumpyarabot.config import settings


async def get_jenkins_builds(job_name: str) -> List[schemas.JenkinsBuild]:
    """Fetch all builds from Jenkins for a specific job."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.JENKINS_URL}/job/{job_name}/api/json",
            params={"tree": "allBuilds[number,result,actions[parameters[name,value]]]"},
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
        )
        response.raise_for_status()
        return [schemas.JenkinsBuild(**build) for build in response.json()["allBuilds"]]


async def check_existing_build(args: schemas.DumpArguments) -> Tuple[bool, str]:
    """Check if a build with the given parameters already exists."""
    job_name = "privdump" if args.use_privdump else "dumpyara"
    builds = await get_jenkins_builds(job_name)

    for build in builds:
        if _is_matching_build(build, args):
            return _get_build_status(build)

    return False, f"No matching build found. A new {job_name} build will be started."


def _is_matching_build(
    build: schemas.JenkinsBuild, args: schemas.DumpArguments
) -> bool:
    """Check if a build matches the given arguments."""
    for action in build.actions:
        if "parameters" in action:
            params = {param["name"]: param["value"] for param in action["parameters"]}
            return (
                params.get("URL") == args.url.unicode_string()
                and params.get("USE_ALT_DUMPER") == args.use_alt_dumper
                and params.get("ADD_BLACKLIST") == args.add_blacklist
            )
    return False


def _get_build_status(build: schemas.JenkinsBuild) -> Tuple[bool, str]:
    """Get the status of a build."""
    if build.result is None:
        return (
            True,
            f"Build #{build.number} is currently in progress for this URL and settings.",
        )
    elif build.result == "SUCCESS":
        return (
            True,
            f"Build #{build.number} has already successfully completed for this URL and settings.",
        )
    else:
        return (
            False,
            f"Build #{build.number} exists for this URL and settings, but result was {build.result}. A new build will be started.",
        )


async def call_jenkins(args: schemas.DumpArguments) -> str:
    """Call Jenkins to start a new build."""
    job_name = "privdump" if args.use_privdump else "dumpyara"
    async with httpx.AsyncClient() as client:
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
        return f"{job_name.capitalize()} job started"


async def cancel_jenkins_job(job_id: str, use_privdump: bool = False) -> str:
    """Cancel a Jenkins job."""
    job_name = "privdump" if use_privdump else "dumpyara"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.JENKINS_URL}/job/{job_name}/{job_id}/stop",
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
            follow_redirects=True,
        )
        if response.status_code == 200:
            return f"Job with ID {job_id} has been cancelled in {job_name}."
        elif response.status_code == 404:
            response = await client.post(
                f"{settings.JENKINS_URL}/queue/cancelItem",
                params={"id": job_id},
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                follow_redirects=True,
            )
            if response.status_code == 204:
                return (
                    f"Job with ID {job_id} has been removed from the {job_name} queue."
                )

        return f"Failed to cancel job with ID {job_id} in {job_name}. Job not found or already completed."
