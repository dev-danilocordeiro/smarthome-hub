"""Seed homes and pairing codes for a simulated fleet (development only).

Creates one home per home in the simulator's plan, owned by a Keycloak dev user, and one
pairing code per planned device. The simulator then pairs each device over HTTP exactly
like real hardware would (`python -m smarthome_simulator claim`).

    python -m smarthome.devtools.fleet seed PLAN.json PAIRING.json [--owner alice]
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from device_protocol import DeviceKind
from smarthome.modules.devices import wiring as devices
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.model import UserId
from smarthome.modules.identity.domain.principal import Principal
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.redis import create_redis


async def keycloak_user(settings: Settings, username: str) -> Principal:
    """Look the dev user up through Keycloak's admin API (dev credentials from .env)."""
    base = settings.oidc_discovery_url.split("/realms/")[0]
    async with httpx.AsyncClient(timeout=10) as http:
        token = (
            await http.post(
                f"{base}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": os.environ.get("KEYCLOAK_ADMIN", "admin"),
                    "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
                },
            )
        ).json()["access_token"]
        users = (
            await http.get(
                f"{base}/admin/realms/smarthome/users",
                params={"username": username, "exact": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()
    if not users:
        raise SystemExit(f"no Keycloak user {username!r} in realm smarthome")
    user = users[0]
    return Principal(
        user_id=UserId(user["id"]),
        email=user.get("email"),
        display_name=f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() or None,
        authenticated_at=datetime.now(UTC),
    )


async def seed(plan: dict[str, Any], settings: Settings, owner: str) -> dict[str, Any]:
    engine = create_engine(settings)
    redis = create_redis(settings)
    clock = SystemClock()
    try:
        principal = await keycloak_user(settings, owner)
        identity = IdentityService(lambda: PostgresUnitOfWork(engine), clock)
        await identity.record_login(principal)
        devices_service = devices.build_service(settings, engine=engine, redis=redis, clock=clock)
        homes = []
        for planned in plan["homes"]:
            home = await identity.create_home(
                principal, name=planned["name"], timezone="America/Sao_Paulo"
            )
            codes = []
            for device in planned["devices"]:
                kind = DeviceKind(device["type"])
                name = f"{device['room'].replace('_', ' ').title()} {kind.value.replace('_', ' ')}"
                _, code = await devices_service.create_pairing_code(
                    home_id=home.id,
                    actor=principal.user_id,
                    name=name,
                    room=device["room"],
                    kind=kind,
                )
                codes.append(
                    {"type": kind.value, "room": device["room"], "name": name, "code": code}
                )
            homes.append({"home_id": str(home.id), "name": home.name, "devices": codes})
        return {"seed": plan.get("seed", 0), "owner": owner, "homes": homes}
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="smarthome.devtools.fleet")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("seed", help="create homes and pairing codes for a simulator plan")
    cmd.add_argument("plan", type=Path)
    cmd.add_argument("pairing", type=Path)
    cmd.add_argument("--owner", default="alice", help="Keycloak username that will own the homes")
    args = parser.parse_args()

    plan: dict[str, Any] = json.loads(args.plan.read_text())
    pairing = asyncio.run(seed(plan, Settings(), args.owner))
    args.pairing.write_text(json.dumps(pairing, indent=2))
    count = sum(len(h["devices"]) for h in pairing["homes"])
    print(f"created {len(pairing['homes'])} homes owned by {args.owner} and {count} pairing codes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
