from pydantic import ValidationError
from rich.console import Console
from telegram import Update
from telegram.ext import ContextTypes

from dumpyarabot import schemas, utils
from dumpyarabot.config import settings

console = Console()


async def dump_main(
    update: Update, context: ContextTypes.DEFAULT_TYPE, use_alt_dumper: bool = False
) -> None:
    # Just here to keep mypy happy
    if (
        update.effective_chat is None
        or update.effective_message is None
        or update.effective_user is None
    ):
        raise Exception("What happened here?")

    # Ensure it can only be used in the correct group
    if update.effective_chat.id not in settings.ALLOWED_CHATS:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.effective_message.id,
            text="You can't use this here",
        )
        return

    # Ensure that we had some arguments passed
    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.effective_message.id,
            text="Please pass in a URL",
        )
        return

    # Try to call jenkins
    try:
        response_text = await utils.call_jenkins(
            schemas.DumpArguments(url=context.args[0], use_alt_dumper=use_alt_dumper)
        )
    except ValidationError:
        response_text = "Invalid URL"
    except Exception:
        response_text = "Exception occurred"
        console.print_exception(show_locals=True)

    # Reply to the user with whatever the status is
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        reply_to_message_id=update.effective_message.id,
        text=response_text,
    )


async def dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await dump_main(update, context, use_alt_dumper=False)


async def dump_alt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await dump_main(update, context, use_alt_dumper=True)


async def cancel_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Just here to keep mypy happy
    if (
        update.effective_chat is None
        or update.effective_message is None
        or update.effective_user is None
    ):
        raise Exception("What happened here?")

    if (
        update.effective_chat.id not in settings.ALLOWED_CHATS
        or update.effective_user
        not in (
            admin.user for admin in await update.effective_chat.get_administrators()
        )
    ):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.effective_message.id,
            text="You can't use this here",
        )
        return

    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.effective_message.id,
            text="Please provide a job ID.",
        )
        return

    job_id = context.args[0]
    response_message = await utils.cancel_jenkins_job(job_id)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        reply_to_message_id=update.effective_message.id,
        text=response_message,
    )
