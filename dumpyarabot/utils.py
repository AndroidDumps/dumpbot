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
        response = await client.post(
            f"{settings.JENKINS_URL}/job/dumpyara/buildWithParameters",
            params=httpx.QueryParams(
                {
                    "URL": args.url.unicode_string(),
                    "USE_ALT_DUMPER": args.use_alt_dumper,
                }
            ),
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
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
            auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
            follow_redirects=True,
        )
        if response.status_code == 200:
            return f"Job with ID {job_id} has been cancelled."
        elif response.status_code == 404:
            response = await client.post(
                f"{settings.JENKINS_URL}/queue/cancelItem",
                params=httpx.QueryParams({"id": job_id}),
                auth=(settings.JENKINS_USER_NAME, settings.JENKINS_USER_TOKEN),
                follow_redirects=True,
            )
            if response.status_code == 204:
                return f"Job with ID {job_id} has been removed from the queue."
        print(response.text)
        return f"Failed to cancel job with ID {job_id}. Status code: {response.status_code}. Check stdout for more details."
