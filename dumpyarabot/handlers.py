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
        console.print("[red]Chat or message object is None[/red]")
        return

    # Ensure it can only be used in the correct group
    if chat.id not in settings.ALLOWED_CHATS:
        console.print(
            f"[yellow]Unauthorized chat attempt from chat_id: {chat.id}[/yellow]"
        )
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You can't use this here",
        )
        return

    # Ensure that we had some arguments passed
    if not context.args:
        console.print("[yellow]No arguments provided for dump command[/yellow]")
        usage = "Usage: `/dump [URL] [a|f|b|p]`\nURL: required, a: alt dumper, f: force, b: blacklist, p: use privdump"
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text=usage,
            parse_mode="Markdown",
        )
        return

    url = context.args[0]
    options = "".join("".join(context.args[1:]).split())

    use_alt_dumper = "a" in options
    force = "f" in options
    add_blacklist = "b" in options
    use_privdump = "p" in options

    console.print(f"[green]Dump request:[/green]")
    console.print(f"  URL: {url}")
    console.print(f"  Alt dumper: {use_alt_dumper}")
    console.print(f"  Force: {force}")
    console.print(f"  Blacklist: {add_blacklist}")
    console.print(f"  Privdump: {use_privdump}")

    # Delete the user's message immediately if privdump is used
    if use_privdump:
        console.print(
            f"[blue]Privdump requested - deleting message {message.message_id}[/blue]"
        )
        try:
            await context.bot.delete_message(
                chat_id=chat.id, message_id=message.message_id
            )
            console.print(
                "[green]Successfully deleted original message for privdump[/green]"
            )
        except Exception as e:
            console.print(f"[red]Failed to delete message for privdump: {e}[/red]")

    # Try to check for existing build and call jenkins if necessary
    try:
        dump_args = schemas.DumpArguments(
            url=url,
            use_alt_dumper=use_alt_dumper,
            add_blacklist=add_blacklist,
            use_privdump=use_privdump,
        )

        if not force:
            console.print("[blue]Checking for existing builds...[/blue]")
            initial_message = await context.bot.send_message(
                chat_id=chat.id,
                reply_to_message_id=None if use_privdump else message.message_id,
                text="Checking for existing builds...",
            )

            exists, status_message = await utils.check_existing_build(dump_args)
            if exists:
                console.print(
                    f"[yellow]Found existing build: {status_message}[/yellow]"
                )
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

        console.print("[blue]Calling Jenkins to start build...[/blue]")
        response_text = await utils.call_jenkins(dump_args)
        console.print(f"[green]Jenkins response: {response_text}[/green]")

    except ValidationError:
        console.print(f"[red]Invalid URL provided: {url}[/red]")
        response_text = "Invalid URL"
    except Exception:
        console.print("[red]Unexpected error occurred:[/red]")
        console.print_exception()
        response_text = "An error occurred"

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
        console.print("[red]Chat, message or user object is None[/red]")
        return

    # Ensure it can only be used in the correct group
    if chat.id not in settings.ALLOWED_CHATS:
        console.print(
            f"[yellow]Unauthorized chat attempt for cancel from chat_id: {chat.id}[/yellow]"
        )
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You can't use this here",
        )
        return

    # Check if the user is an admin
    admins = await chat.get_administrators()
    if user not in [admin.user for admin in admins]:
        console.print(
            f"[yellow]Non-admin user {user.id} tried to use cancel command[/yellow]"
        )
        await context.bot.send_message(
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            text="You don't have permission to use this command",
        )
        return

    # Ensure that we had some arguments passed
    if not context.args:
        console.print("[yellow]No job_id provided for cancel command[/yellow]")
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

    console.print(f"[blue]Cancel request:[/blue]")
    console.print(f"  Job ID: {job_id}")
    console.print(f"  Privdump: {use_privdump}")
    console.print(f"  Requested by: {user.username} (ID: {user.id})")

    try:
        response_message = await utils.cancel_jenkins_job(job_id, use_privdump)
        console.print(
            f"[green]Successfully processed cancel request: {response_message}[/green]"
        )
    except Exception as e:
        console.print("[red]Error processing cancel request:[/red]")
        console.print_exception()
        response_message = f"Error cancelling job: {str(e)}"

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
