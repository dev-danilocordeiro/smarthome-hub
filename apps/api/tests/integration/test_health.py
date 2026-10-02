import httpx
import pytest

from smarthome.shared.config import Settings
from tests.integration.conftest import client_for

# Nothing listens on port 1; connections are refused immediately.
UNREACHABLE_POSTGRES = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/void"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


async def test_liveness_reports_up_without_touching_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "up"}


async def test_readiness_reports_up_when_postgres_and_redis_are_reachable(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "up", "checks": {"postgres": "up", "redis": "up"}}


@pytest.mark.parametrize(
    ("override", "expected_checks"),
    [
        pytest.param(
            {"redis_url": UNREACHABLE_REDIS},
            {"postgres": "up", "redis": "down"},
            id="redis-down",
        ),
        pytest.param(
            {"database_url": UNREACHABLE_POSTGRES},
            {"postgres": "down", "redis": "up"},
            id="postgres-down",
        ),
    ],
)
async def test_readiness_returns_503_and_names_the_dependency_that_is_down(
    migrated_database: Settings,
    override: dict[str, str],
    expected_checks: dict[str, str],
) -> None:
    # Rebuild rather than model_copy so the override is validated like it would be at startup.
    degraded = Settings.model_validate({**migrated_database.model_dump(), **override})

    async for client in client_for(degraded):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "down", "checks": expected_checks}
