from typing import Optional

from pydantic import ValidationError
from rich.console import Console
from telegram import Chat, Message, Update
from telegram.ext import ContextTypes

from dumpyarabot import schemas, utils
from dumpyarabot.config import settings

console = Console()


async def dump(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handler for the /dump command."""
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
        usage = "Usage: `/dump [URL] [a|f|b|p]`\nURL: required, a: alt dumper, f: force, b: blacklist, p: use privdump"
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text=usage,
            parse_mode="Markdown",
        )
        return

    url = context.args[0]
    use_alt_dumper = "a" in context.args[1:] if len(context.args) > 1 else False
    force = "f" in context.args[1:] if len(context.args) > 1 else False
    add_blacklist = "b" in context.args[1:] if len(context.args) > 1 else False
    use_privdump = "p" in context.args[1:] if len(context.args) > 1 else False

    # Delete the user's message immediately if privdump is used
    if use_privdump:
        await context.bot.delete_message(chat_id=chat.id, message_id=message.message_id)

    # Try to check for existing build and call jenkins if necessary
    try:
        dump_args = schemas.DumpArguments(
            url=url,
            use_alt_dumper=use_alt_dumper,
            add_blacklist=add_blacklist,
            use_privdump=use_privdump,
        )

        if not force:
            initial_message = await context.bot.send_message(
                chat_id=chat.id,
                reply_to_message_id=None if use_privdump else message.message_id,
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
        reply_to_message_id=None if use_privdump else message.message_id,
        text=response_text,
    )


async def cancel_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /cancel command."""
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
        usage = (
            "Usage: `/cancel [job_id] [p]`\njob_id: required, p: cancel privdump job"
        )
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text=usage,
            parse_mode="Markdown",
        )
        return

    job_id = context.args[0]
    use_privdump = "p" in context.args[1:] if len(context.args) > 1 else False

    response_message = await utils.cancel_jenkins_job(job_id, use_privdump)
    await context.bot.send_message(
        chat_id=chat.id,
        reply_to_message_id=message.message_id,
        text=response_message,
    )


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    context.application.stop_running()
    context.bot_data["restart"] = True
