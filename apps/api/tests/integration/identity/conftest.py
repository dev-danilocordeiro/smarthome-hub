import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer

from smarthome.shared.config import Settings
from tests.integration.conftest import client_for
from tests.integration.identity.browser import Browser

# Keep in sync with infra/docker-compose.yml.
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.8.0"
APP_URL = "https://api"  # where the test browser reaches the BFF
WEB_URL = "https://app"  # the SPA origin
STARTUP_TIMEOUT = 180


@pytest.fixture(scope="session")
def keycloak_url(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> Iterator[str]:
    realm = json.loads((repo_root / "infra/keycloak/realm-export.json").read_text())
    bff = next(c for c in realm["clients"] if c["clientId"] == "smarthome-bff")
    bff["redirectUris"].append(f"{APP_URL}/auth/callback")
    bff["attributes"]["post.logout.redirect.uris"] += f"##{WEB_URL}/*"
    realm_file = tmp_path_factory.mktemp("keycloak") / "realm-export.json"
    realm_file.write_text(json.dumps(realm))
    realm_file.chmod(0o644)

    container = (
        DockerContainer(KEYCLOAK_IMAGE)
        .with_command("start-dev --import-realm")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin")
        .with_exposed_ports(8080)
        .with_volume_mapping(str(realm_file), "/opt/keycloak/data/import/realm-export.json", "ro")
    )
    with container:
        base = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        discovery = f"{base}/realms/smarthome/.well-known/openid-configuration"
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            try:
                if httpx.get(discovery, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"Keycloak did not start: {container.get_logs()}")
            time.sleep(1)
        yield base


@pytest.fixture
def oidc_settings(migrated_database: Settings, keycloak_url: str) -> Settings:
    discovery = f"{keycloak_url}/realms/smarthome/.well-known/openid-configuration"
    return Settings.model_validate(
        migrated_database.model_dump()
        | {
            "oidc_discovery_url": discovery,
            "bff_public_url": APP_URL,
            "web_app_url": WEB_URL,
        }
    )


@pytest.fixture
async def app_client(oidc_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(oidc_settings, base_url=APP_URL):
        yield client


@pytest.fixture
async def browser(app_client: httpx.AsyncClient) -> AsyncIterator[Browser]:
    async with httpx.AsyncClient(timeout=10) as network:
        yield Browser(app=app_client, network=network, app_url=APP_URL)
