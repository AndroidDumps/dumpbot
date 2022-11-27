from pydantic import AnyHttpUrl, BaseSettings


class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str

    JENKINS_TOKEN: str

    JENKINS_URL: AnyHttpUrl

    SUDO_USERS: list[int] = []

    ALLOWED_CHATS: list[int]

    class Config:
        env_file = ".env"


settings = Settings()
