import pytest
from pydantic import ValidationError

from smarthome.shared.config import Environment, Settings


def test_settings_are_read_from_smarthome_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMARTHOME_ENVIRONMENT", "production")
    monkeypatch.setenv("SMARTHOME_REDIS_URL", "redis://cache:6380/2")

    settings = Settings()

    assert settings.environment is Environment.PRODUCTION
    assert str(settings.redis_url) == "redis://cache:6380/2"


def test_a_database_url_that_is_not_postgres_is_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMARTHOME_DATABASE_URL", "mysql://user:pw@db/app")

    with pytest.raises(ValidationError):
        Settings()


def test_a_non_positive_readiness_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMARTHOME_READINESS_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValidationError):
        Settings()
