from typing import Optional

from pydantic import ValidationError
from rich.console import Console
from telegram import Chat, Message, Update
from telegram.ext import ContextTypes

from dumpyarabot import schemas, utils
from dumpyarabot.config import settings

console = Console()


async def dump_main(
    update: Update, context: ContextTypes.DEFAULT_TYPE, use_alt_dumper: bool = False
) -> None:
    """Main handler for the /dump and /dump_alt commands."""
    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message

    if not chat or not message:
        return

    # Ensure it can only be used in the correct group
    if chat.id not in settings.ALLOWED_CHATS:
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You can't use this here",
        )
        return

    # Ensure that we had some arguments passed
    if not context.args:
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="Please pass in a URL",
        )
        return

    # Try to check for existing build and call jenkins if necessary
    try:
        dump_args = schemas.DumpArguments(
            url=context.args[0], use_alt_dumper=use_alt_dumper
        )
        initial_message = await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="Checking for existing builds...",
        )

        exists, status_message = await utils.check_existing_build(dump_args)
        if exists:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=initial_message.message_id,
                text=status_message,
            )
            return

        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=initial_message.message_id,
        )

        response_text = await utils.call_jenkins(dump_args)
    except ValidationError:
        response_text = "Invalid URL"
    except Exception:
        response_text = "An error occurred"
        console.print_exception(show_locals=True)

    # Reply to the user with whatever the status is
    await context.bot.send_message(
        chat_id=chat.id,
        reply_to_message_id=message.message_id,
        text=response_text,
    )


async def dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /dump command."""
    await dump_main(update, context, use_alt_dumper=True)


async def dump_alt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /dump_alt command."""
    await dump_main(update, context, use_alt_dumper=False)


async def cancel_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /cancel_dump command."""
    chat: Optional[Chat] = update.effective_chat
    message: Optional[Message] = update.effective_message
    user = update.effective_user

    if not chat or not message or not user:
        return

    # Ensure it can only be used in the correct group
    if chat.id not in settings.ALLOWED_CHATS:
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You can't use this here",
        )
        return

    # Check if the user is an admin
    admins = await chat.get_administrators()
    if user not in [admin.user for admin in admins]:
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You don't have permission to use this command",
        )
        return

    # Ensure that we had some arguments passed
    if not context.args:
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="Please provide a job ID.",
        )
        return

    job_id = context.args[0]
    response_message = await utils.cancel_jenkins_job(job_id)
    await context.bot.send_message(
        chat_id=chat.id,
        reply_to_message_id=message.message_id,
        text=response_message,
    )
