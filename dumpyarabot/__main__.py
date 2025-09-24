import os
import sys

from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters, JobQueue)

from dumpyarabot.handlers import blacklist, cancel_dump, dump, help_command, restart
from dumpyarabot.mockup_handlers import (handle_enhanced_callback_query,
                                         mockup_command)
from dumpyarabot.moderated_handlers import (accept_command,
                                            handle_request_message,
                                            reject_command)

from .config import settings


async def handle_post_restart_update(context):
    """Update the original restart message to confirm successful restart."""
    from dumpyarabot.redis_storage import RedisStorage
    from rich.console import Console
    console = Console()

    restart_info = RedisStorage.get_restart_message_info()

    if restart_info:
        console.print(f"[blue]Found restart message info: {restart_info}[/blue]")

        try:
            # Update the original restart message to confirm success
            await context.bot.edit_message_text(
                chat_id=restart_info["chat_id"],
                message_id=restart_info["message_id"],
                text=f" **Restart Complete**\n\n"
                     f" **Requested by:** {restart_info['user_mention']}\n"
                     f" **Status:** Bot successfully restarted and is now online!\n\n"
                     f"⏱ All operations are ready to resume.",
                parse_mode="Markdown"
            )

            console.print("[green]Successfully updated restart confirmation message[/green]")

        except Exception as e:
            console.print(f"[yellow]Could not update restart message: {e}[/yellow]")

        finally:
            # Clean up restart context
            RedisStorage.clear_restart_message_info()
    else:
        console.print("[yellow]No restart message info found in Redis[/yellow]")


async def register_bot_commands(application):
    """Register bot commands with Telegram for the menu interface."""
    from dumpyarabot.config import USER_COMMANDS
    from telegram import BotCommand

    # Register user commands (visible to all users)
    commands = [BotCommand(cmd, desc) for cmd, desc in USER_COMMANDS]
    application.bot.delete_my_commands

if __name__ == "__main__":
    application = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).job_queue(JobQueue()).build()
    application.bot_data["restart"] = False

    # Existing handlers
    dump_handler = CommandHandler("dump", dump)
    blacklist_handler = CommandHandler("blacklist", blacklist)
    cancel_dump_handler = CommandHandler("cancel", cancel_dump)
    help_handler = CommandHandler("help", help_command)

    # Mockup handler for testing UI flow
    mockup_handler = CommandHandler("mockup", mockup_command)

    # Moderated request system handlers
    accept_handler = CommandHandler("accept", accept_command)
    reject_handler = CommandHandler("reject", reject_command)
    request_message_handler = MessageHandler(
        filters.TEXT & filters.Regex(r"#request"), handle_request_message
    )
    # Use enhanced callback handler that supports both production and mockup callbacks
    callback_handler = CallbackQueryHandler(handle_enhanced_callback_query)

    # Restart handler - now fully implemented
    restart_handler = CommandHandler("restart", restart)


    # Add all handlers
    application.add_handler(dump_handler)
    application.add_handler(blacklist_handler)
    application.add_handler(cancel_dump_handler)
    application.add_handler(help_handler)
    application.add_handler(mockup_handler)
    application.add_handler(accept_handler)
    application.add_handler(reject_handler)
    application.add_handler(request_message_handler)
    application.add_handler(callback_handler)
    application.add_handler(restart_handler)

    # Register bot commands on startup (if job queue is available)
    if application.job_queue:
        application.job_queue.run_once(
            lambda context: register_bot_commands(application), 0
        )
        # Handle post-restart message update
        application.job_queue.run_once(handle_post_restart_update, 1)

    application.run_polling()

    if application.bot_data["restart"]:
        os.execl(sys.executable, sys.executable, "-m", "dumpyarabot")
