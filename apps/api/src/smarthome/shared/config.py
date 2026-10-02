from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process configuration. Every field maps to an `SMARTHOME_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="SMARTHOME_", extra="ignore", frozen=True)

    service_name: str = "smarthome-api"
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_json: bool = True

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://smarthome:smarthome-dev-only@localhost:15432/smarthome"
    )
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)

    redis_url: RedisDsn = RedisDsn("redis://localhost:16379/0")

    # Upper bound for each dependency probe in /health/ready. Keeps the endpoint
    # fast enough for orchestrator probes even when a dependency hangs.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
