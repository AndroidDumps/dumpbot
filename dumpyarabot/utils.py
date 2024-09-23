import httpx

from dumpyarabot import schemas
from dumpyarabot.config import settings


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
