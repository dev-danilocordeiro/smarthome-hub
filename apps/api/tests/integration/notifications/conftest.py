"""A household (owner, resident, viewer, guest) with a battery sensor, a lock and a
meter; the alert engine and dispatcher over real Postgres; Mailpit as the SMTP server;
a local HTTP receiver for webhooks."""

import json
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from device_protocol import DeviceKind
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.energy import wiring as energy
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.model import HomeId, Role
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.modules.notifications import wiring as notifications
from smarthome.modules.notifications.application.alerts import AlertEngine
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from tests.integration.automations.conftest import publisher, stream
from tests.integration.energy.conftest import api, device_service
from tests.integration.helpers import Member, sign_in

if TYPE_CHECKING:
    from smarthome.modules.notifications.application.dispatch import DeliveryDispatcher

__all__ = ["api", "device_service", "publisher", "stream"]

MAILPIT_IMAGE = "axllent/mailpit:v1.31.4"


class MovableClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.at = now or datetime.now(UTC)

    def now(self) -> datetime:
        return self.at


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@dataclass(frozen=True)
class Household:
    home: HomeId
    identity: IdentityService
    owner: Member
    resident: Member
    viewer: Member
    guest: Member
    devices: dict[str, str]


async def _join(
    redis: Redis, identity: IdentityService, owner: Member, home: HomeId, role: Role, **kw: Any
) -> Member:
    joined = await sign_in(redis, f"{role.value}-{secrets.token_hex(4)}")
    _, token = await identity.invite(owner.principal, home, role=role, **kw)
    await identity.accept_invitation(joined.principal, token)
    return joined


@pytest.fixture
async def household(engine: AsyncEngine, redis: Redis, device_service: DevicesService) -> Household:
    identity = IdentityService(lambda: PostgresUnitOfWork(engine), SystemClock())
    owner = await sign_in(redis, f"owner-{secrets.token_hex(4)}")
    home = (
        await identity.create_home(owner.principal, name="Casa", timezone="America/Sao_Paulo")
    ).id
    devices: dict[str, str] = {}
    for name, kind in {
        "sensor": DeviceKind.MOTION_SENSOR,
        "lock": DeviceKind.LOCK,
        "meter": DeviceKind.ENERGY_METER,
    }.items():
        _, code = await device_service.create_pairing_code(
            home_id=home, actor=owner.principal.user_id, name=name.title()
        )
        devices[name] = (await device_service.claim(code=code, kind=kind, firmware=None)).device.id
    resident = await _join(redis, identity, owner, home, Role.RESIDENT)
    viewer = await _join(redis, identity, owner, home, Role.VIEWER)
    guest = await _join(
        redis,
        identity,
        owner,
        home,
        Role.GUEST,
        guest_access_expires_at=datetime.now(UTC) + timedelta(days=1),
        device_scope=frozenset({devices["lock"]}),
    )
    # The IdP gave owner and resident an email when they signed in; the viewer has none.
    for member in (owner, resident):
        email = f"{member.principal.user_id}@example.com"
        await identity.record_login(replace(member.principal, email=email))
    return Household(home, identity, owner, resident, viewer, guest, devices)


@pytest.fixture
def alert_engine(
    migrated_database: Settings,
    engine: AsyncEngine,
    device_service: DevicesService,
    clock: MovableClock,
) -> AlertEngine:
    return notifications.build_engine(
        migrated_database.model_copy(update={"alert_offline_grace_s": 60}),
        engine=engine,
        identity=IdentityService(lambda: PostgresUnitOfWork(engine), clock),
        devices=device_service,
        energy=energy.build_service(engine=engine, devices=device_service, clock=clock),
        clock=clock,
    )


@pytest.fixture(scope="session")
def mailpit() -> Iterator[str]:
    """Base URL of Mailpit's HTTP API; SMTP is on the mapped port 1025."""
    container = (
        DockerContainer(MAILPIT_IMAGE)
        .with_exposed_ports(1025, 8025)
        .waiting_for(HttpWaitStrategy(8025, "/livez"))
    )
    with container:
        yield (
            f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8025)}"
            f"|{container.get_exposed_port(1025)}"
        )


@dataclass
class Receiver:
    """A webhook endpoint on localhost that records what it gets and answers with
    `status` (a list: one status per request, the last one repeats)."""

    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[int] = field(default_factory=lambda: [204])


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    state = Receiver(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append(
                {"headers": dict(self.headers), "body": body, "json": json.loads(body)}
            )
            code = state.statuses.pop(0) if len(state.statuses) > 1 else state.statuses[0]
            self.send_response(code)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}/hooks/smarthome"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
async def dispatcher(
    migrated_database: Settings, engine: AsyncEngine, mailpit: str, clock: MovableClock
) -> Any:
    api_url, smtp_port = mailpit.split("|")
    settings = migrated_database.model_copy(
        update={"smtp_host": api_url.split("//")[1].split(":")[0], "smtp_port": int(smtp_port)}
    )
    async with engine.begin() as conn:
        # The queue is shared by every test in the session: start this one empty.
        await conn.execute(
            text(
                "UPDATE notifications.deliveries SET status = 'skipped',"
                " next_attempt_at = NULL, settled_at = now() WHERE status = 'queued'"
            )
        )
    async with notifications.webhook_client() as http:
        built: DeliveryDispatcher = notifications.build_dispatcher(
            settings, engine=engine, http=http, clock=clock
        )
        yield built


async def mailbox(mailpit: str, to: str) -> list[dict[str, Any]]:
    api_url = mailpit.split("|", maxsplit=1)[0]
    async with httpx.AsyncClient() as client:
        found = await client.get(f"{api_url}/api/v1/search", params={"query": f"to:{to}"})
        return list(found.json()["messages"])
