from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str

    JENKINS_URL: AnyHttpUrl
    JENKINS_USER_NAME: str
    JENKINS_USER_TOKEN: str

    SUDO_USERS: list[int] = []

    ALLOWED_CHATS: list[int]
    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
