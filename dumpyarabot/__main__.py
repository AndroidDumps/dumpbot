import os
import sys

from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters, JobQueue)

from dumpyarabot.handlers import cancel_dump, dump, help_command, restart, status
from dumpyarabot.message_queue import message_queue
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

    restart_info = await RedisStorage.get_restart_message_info()

    if restart_info:
        console.print(f"[blue]Found restart message info: {restart_info}[/blue]")

        try:
            # Edit the existing confirmation message after startup.
            await message_queue.send_status_update(
                chat_id=restart_info["chat_id"],
                text=f" *Restart Complete*\n\n"
                     f" *Requested by:* {restart_info['user_mention']}\n"
                     f" *Status:* Bot successfully restarted and is now online!\n\n"
                     f"⏱ All operations are ready to resume.",
                edit_message_id=restart_info["message_id"],
                parse_mode=settings.DEFAULT_PARSE_MODE,
                context={"restart_completion": True}
            )

            console.print("[green]Queued restart confirmation edit[/green]")

        except Exception as e:
            console.print(f"[yellow]Could not queue restart confirmation edit: {e}[/yellow]")

        finally:
            # Clean up restart context
            await RedisStorage.clear_restart_message_info()
    else:
        console.print("[yellow]No restart message info found in Redis[/yellow]")


async def initialize_message_queue(context):
    """Initialize the message queue system."""
    from rich.console import Console
    console = Console()

    try:
        # Set the bot instance for the message queue
        message_queue.set_bot(context.bot)

        # Start the message consumer
        await message_queue.start_consumer()

        console.print("[green]Message queue system initialized successfully[/green]")
    except Exception as e:
        console.print(f"[red]Failed to initialize message queue: {e}[/red]")


async def initialize_arq(context):
    """Initialize the ARQ system."""
    from dumpyarabot.arq_config import init_arq
    from rich.console import Console
    console = Console()

    try:
        await init_arq()
        console.print("[green]ARQ system initialized successfully[/green]")
    except Exception as e:
        console.print(f"[red]Failed to initialize ARQ: {e}[/red]")


async def register_bot_commands(application):
    """Register bot commands with Telegram for the menu interface."""
    from dumpyarabot.config import USER_COMMANDS
    from telegram import BotCommand

    # Register user commands (visible to all users)
    commands = [BotCommand(cmd, desc) for cmd, desc in USER_COMMANDS]
    await application.bot.delete_my_commands()
    await application.bot.set_my_commands(commands)


async def register_bot_commands_job(context):
    """JobQueue adapter for bot command registration."""
    await register_bot_commands(context.application)

if __name__ == "__main__":
    application = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).job_queue(JobQueue()).build()
    application.bot_data["restart"] = False

    # Existing handlers
    dump_handler = CommandHandler("dump", dump)
    cancel_dump_handler = CommandHandler("cancel", cancel_dump)
    status_handler = CommandHandler("status", status)
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
    application.add_handler(cancel_dump_handler)
    application.add_handler(status_handler)
    application.add_handler(help_handler)
    application.add_handler(mockup_handler)
    application.add_handler(accept_handler)
    application.add_handler(reject_handler)
    application.add_handler(request_message_handler)
    application.add_handler(callback_handler)
    application.add_handler(restart_handler)

    # Register bot commands on startup (if job queue is available)
    if application.job_queue:
        application.job_queue.run_once(initialize_arq, 1)

        # Initialize message queue system
        application.job_queue.run_once(initialize_message_queue, 2)

        application.job_queue.run_once(register_bot_commands_job, 3)
        # Handle post-restart message update
        application.job_queue.run_once(handle_post_restart_update, 4)

    application.run_polling()

    if application.bot_data["restart"]:
        # Graceful cleanup before restart
        import asyncio
        async def _shutdown():
            try:
                await message_queue.stop_consumer()
            except Exception:
                pass
            try:
                from dumpyarabot.arq_config import shutdown_arq
                await shutdown_arq()
            except Exception:
                pass
        asyncio.run(_shutdown())
        os.execl(sys.executable, sys.executable, "-m", "dumpyarabot")
