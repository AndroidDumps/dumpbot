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
            params=(
                ("token", settings.JENKINS_TOKEN),
                ("URL", args.url),
                ("USE_ALT_DUMPER", args.use_alt_dumper)
            ),
        )
        if response.status_code in (200, 201):
            return "Job started"
        return response.text
