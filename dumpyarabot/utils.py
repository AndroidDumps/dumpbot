import httpx

from dumpyarabot import schemas
from dumpyarabot.config import settings


async def call_jenkins(args: schemas.DumpArguments) -> str:
    """
    Function to call jenkins

    :param args: The schema for the jenkins call
    :return: A reply for the user
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.JENKINS_URL}/job/dumpyara/buildWithParameters",
            params=httpx.QueryParams(
                {
                    "token": settings.JENKINS_TOKEN,
                    "URL": args.url.unicode_string(),
                    "USE_ALT_DUMPER": args.use_alt_dumper,
                }
            ),
        )
        if response.status_code in (200, 201):
            return "Job started"
        return response.text


async def cancel_jenkins_job(job_id: str) -> str:
    """
    Function to cancel a Jenkins job.

    :param job_id: The ID of the Jenkins job to cancel
    :return: A reply for the user
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.JENKINS_URL}/job/dumpyara/{job_id}/stop",
            params=httpx.QueryParams({"token": settings.JENKINS_TOKEN}),
        )
        if response.status_code == 200:
            return f"Job with ID {job_id} has been cancelled."
        else:
            return f"Failed to cancel job with ID {job_id}. Status code: {response.status_code}, Response: {response.text}"
