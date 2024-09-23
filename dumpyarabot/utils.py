from typing import List, Tuple

import httpx

from dumpyarabot import schemas
from dumpyarabot.config import settings


async def get_jenkins_builds() -> List[schemas.JenkinsBuild]:
    """Fetch all builds from Jenkins."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.JENKINS_URL}/job/dumpyara/api/json",
            params={"tree": "allBuilds[number,result,actions[parameters[name,value]]]"},
        )
        response.raise_for_status()
        return [schemas.JenkinsBuild(**build) for build in response.json()["allBuilds"]]


async def check_existing_build(args: schemas.DumpArguments) -> Tuple[bool, str]:
    """Check if a build with the given parameters already exists."""
    builds = await get_jenkins_builds()

    for build in builds:
        if _is_matching_build(build, args):
            return _get_build_status(build)

    return False, "No matching build found. A new build will be started."


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
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.JENKINS_URL}/job/dumpyara/buildWithParameters",
            params={
                "URL": args.url.unicode_string(),
                "USE_ALT_DUMPER": args.use_alt_dumper,
            },
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
        )
        response.raise_for_status()
        return "Job started"


async def cancel_jenkins_job(job_id: str) -> str:
    """Cancel a Jenkins job."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.JENKINS_URL}/job/dumpyara/{job_id}/stop",
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
            follow_redirects=True,
        )
        if response.status_code == 200:
            return f"Job with ID {job_id} has been cancelled."
        elif response.status_code == 404:
            response = await client.post(
                f"{settings.JENKINS_URL}/queue/cancelItem",
                params={"id": job_id},
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                follow_redirects=True,
            )
            if response.status_code == 204:
                return f"Job with ID {job_id} has been removed from the queue."
        return f"Failed to cancel job with ID {job_id}. Status code: {response.status_code}."
