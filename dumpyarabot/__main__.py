from telegram.ext import ApplicationBuilder, CommandHandler

from dumpyarabot.handlers import dump

from .config import settings

if __name__ == "__main__":
    application = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).build()

    dump_handler = CommandHandler("dump", dump)
    application.add_handler(dump_handler)

    application.run_polling()
