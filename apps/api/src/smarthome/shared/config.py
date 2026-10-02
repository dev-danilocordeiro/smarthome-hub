from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process configuration. Every field maps to an `SMARTHOME_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="SMARTHOME_", extra="ignore", frozen=True)

    service_name: str = "smarthome-api"
    service_version: str = "0.1.0"
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

    # OpenTelemetry. Off by default so a bare process never blocks on a missing collector;
    # infra/docker-compose.observability.yml turns it on.
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_exporter_otlp_insecure: bool = True
    otel_traces_sample_ratio: float = Field(default=1.0, ge=0, le=1)
    otel_metric_export_interval_ms: int = Field(default=10_000, ge=1_000)

    # /diagnostics/* exists to exercise the telemetry pipeline. Never enable in production.
    diagnostics_enabled: bool = False

    # OpenID Connect (Keycloak). The discovery URL is how *this process* reaches the IdP;
    # the issuer inside it is the browser-facing URL (see infra/docker-compose.yml).
    oidc_discovery_url: str = (
        "http://localhost:8080/realms/smarthome/.well-known/openid-configuration"
    )
    oidc_client_id: str = "smarthome-bff"
    oidc_client_secret: SecretStr = SecretStr("dev-only-bff-secret-change-me")

    # Where the browser reaches the BFF (the web app proxies /api to this service) and
    # the SPA itself. Both are allowed origins for unsafe requests.
    bff_public_url: str = "http://localhost:5173/api"
    web_app_url: str = "http://localhost:5173"
    session_absolute_lifetime_hours: int = Field(default=12, ge=1, le=24 * 30)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
