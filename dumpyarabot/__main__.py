import os
import sys

from telegram.ext import ApplicationBuilder, CommandHandler

from dumpyarabot.handlers import cancel_dump, dump

from .config import settings

if __name__ == "__main__":
    application = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).build()
    application.bot_data["restart"] = False

    dump_handler = CommandHandler("dump", dump)
    cancel_dump_handler = CommandHandler("cancel", cancel_dump)
    # TODO: Fix the restart handler implementation
    # restart_handler = CommandHandler("restart", restart)
    application.add_handler(dump_handler)
    application.add_handler(cancel_dump_handler)
    # application.add_handler(restart_handler)

    application.run_polling()

    if application.bot_data["restart"]:
        os.execl(sys.executable, sys.executable, *sys.argv)
