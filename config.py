from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    TELEGRAM_CHAT_ID: str = "5067261557"
    # UPSTASH_REDIS_REST_URL: str
    # UPSTASH_REDIS_REST_TOKEN: str

    class Config:
        env_file = ".env"

settings = Settings()