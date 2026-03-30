from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str

    JENKINS_URL: AnyHttpUrl
    JENKINS_USER_NAME: str
    JENKINS_USER_TOKEN: str

    SUDO_USERS: list[int] = []

    ALLOWED_CHATS: list[int] = []

    # Moderated request system configuration
    REQUEST_CHAT_ID: int = -1001234567890  # Configure actual chat ID
    REVIEW_CHAT_ID: int = -1001234567891  # Configure actual chat ID

    # Redis configuration for persistent storage
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_KEY_PREFIX: str = "dumpyarabot:"

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()

# Callback data prefixes
CALLBACK_ACCEPT = "accept_"
CALLBACK_REJECT = "reject_"
CALLBACK_TOGGLE_ALT = "toggle_alt_"
CALLBACK_TOGGLE_FORCE = "toggle_force_"
CALLBACK_TOGGLE_BLACKLIST = "toggle_blacklist_"
CALLBACK_TOGGLE_PRIVDUMP = "toggle_privdump_"
CALLBACK_SUBMIT_ACCEPTANCE = "submit_accept_"
CALLBACK_CANCEL_REQUEST = "cancel_req_"

# Bot command definitions for Telegram menu
USER_COMMANDS = [
    ("dump", "Start a firmware dump with URL and options"),
    ("blacklist", "Add a URL to the blacklist"),
    ("help", "Show available commands and usage")
]

INTERNAL_COMMANDS = [
    ("cancel", "Cancel a running Jenkins job"),
    ("accept", "Accept a pending dump request"),
    ("reject", "Reject a pending dump request"),
    ("mockup", "Test the moderated request flow")
]

ADMIN_COMMANDS = [
    ("restart", "Restart the bot")
]

EMPTY_COMMANDS = []

# All commands combined
ALL_COMMANDS = USER_COMMANDS + INTERNAL_COMMANDS + ADMIN_COMMANDS

# Restart command configuration
RESTART_CONFIRMATION_TIMEOUT = 30  # seconds
CALLBACK_RESTART_CONFIRM = "restart_confirm_"
CALLBACK_RESTART_CANCEL = "restart_cancel_"

# Jenkins job management
CALLBACK_JENKINS_CANCEL = "jenkins_cancel_"
