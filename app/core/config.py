from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    environment: str = "development"
    app_name: str = "payment-intelligence-platform"
    log_level: str = "INFO"

    database_url: str = Field(..., description="asyncpg connection string")
    database_pool_size: int = 10
    database_max_overflow: int = 20

    stripe_secret_key: SecretStr = Field(..., description="sk_test_...")
    stripe_publishable_key: str = Field(..., description="pk_test_...")
    stripe_webhook_secret: SecretStr = Field(..., description="whsec_...")

    openai_api_key: SecretStr = Field(..., description="sk-...")
    openai_model: str = "gpt-4o-mini"

    redis_url: str = "redis://localhost:6379/0"

    secret_key: SecretStr = Field(..., description="32-byte hex")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def stripe_secret(self) -> str:
        return self.stripe_secret_key.get_secret_value()

    @property
    def stripe_webhook_secret_value(self) -> str:
        return self.stripe_webhook_secret.get_secret_value()

    @property
    def openai_key(self) -> str:
        return self.openai_api_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()