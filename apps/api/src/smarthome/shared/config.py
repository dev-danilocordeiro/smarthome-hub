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

    # MQTT broker (TLS only). The admin account manages device credentials via the
    # dynamic-security plugin; it never publishes device traffic.
    mqtt_host: str = "localhost"
    mqtt_port: int = 8883
    mqtt_ca_file: str = "infra/mqtt/certs/ca.crt"
    mqtt_admin_user: str = "hub-admin"
    mqtt_admin_password: SecretStr = SecretStr("mqtt-admin-dev-only")
    # The hub's own MQTT identity for consuming device traffic (ingestor) and sending
    # commands (worker). Created idempotently by the ingestor at startup.
    mqtt_hub_user: str = "hub-ingestor"
    mqtt_hub_password: SecretStr = SecretStr("mqtt-hub-dev-only")
    # What devices are told to connect to when they pair (differs from mqtt_host inside
    # compose, where the hub reaches the broker as `mqtt`).
    device_broker_host: str = "localhost"
    device_broker_port: int = 8883
    # Pairing claims per client IP per minute. Low in production (codes are guessable in
    # principle); raised in development so a simulated fleet can pair in one go.
    claim_rate_per_minute: int = Field(default=10, ge=1)

    # Telemetry ingestion (ADR 0007).
    telemetry_batch_rows: int = Field(default=500, ge=1)
    telemetry_flush_interval_ms: int = Field(default=1000, ge=50)
    telemetry_buffer_max_rows: int = Field(default=20_000, ge=1)
    # Messages per device per minute before it is quarantined. The busiest simulated
    # device (energy meter) sends 12/min.
    telemetry_max_messages_per_minute: int = Field(default=120, ge=1)
    telemetry_raw_retention_days: int = Field(default=30, ge=8)  # > columnstore delay (7d)
    telemetry_1m_retention_days: int = Field(default=90, ge=1)
    telemetry_1h_retention_days: int = Field(default=730, ge=1)
    telemetry_history_cache_s: int = Field(default=10, ge=0)

    # Commands and the outbox relay (ADR 0009).
    # After a command's deadline, how long to wait for its ack before calling it timed out.
    command_ack_grace_s: int = Field(default=10, ge=0)
    command_sweep_interval_s: float = Field(default=5.0, gt=0)
    outbox_batch_size: int = Field(default=100, ge=1)
    outbox_poll_interval_s: float = Field(default=1.0, gt=0)
    outbox_retention_hours: int = Field(default=24, ge=1)

    # Automations (ADR 0010).
    automation_events_stream_maxlen: int = Field(default=100_000, ge=1_000)
    automation_max_chain_depth: int = Field(default=5, ge=1)
    automation_max_runs_per_minute: int = Field(default=20, ge=1)
    # A schedule slot more than this late (worker was down) is skipped, not run.
    automation_misfire_grace_s: int = Field(default=300, ge=1)
    automation_timer_interval_s: float = Field(default=1.0, gt=0)
    automation_run_retention_days: int = Field(default=30, ge=1)

    # Energy (ADR 0012): how often counter readings are folded into hourly consumption.
    energy_rollup_interval_s: float = Field(default=60.0, gt=0)

    # Live WebSocket (ADR 0011): how often an open socket re-checks session and membership.
    live_recheck_interval_s: float = Field(default=60.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
