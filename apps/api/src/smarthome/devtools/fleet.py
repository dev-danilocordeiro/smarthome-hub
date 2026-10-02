"""Register a simulated fleet with the broker (development only).

Until device pairing exists (phase 5), this stands in for it: it reads the simulator's
fleet plan, gives every device its own random broker password and per-device ACL, and
writes the result for the simulator to use.

    python -m smarthome.devtools.fleet register PLAN.json FLEET.json
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from smarthome.modules.devices.infrastructure.broker_admin import (
    BrokerAdmin,
    BrokerAdminError,
    BrokerConnection,
)
from smarthome.shared.config import Settings


async def register(plan: dict[str, Any], settings: Settings) -> int:
    """Give every device in the plan credentials and an ACL; mutates `plan` in place."""
    admin = BrokerAdmin(
        BrokerConnection(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            ca_file=settings.mqtt_ca_file,
            username=settings.mqtt_admin_user,
            password=settings.mqtt_admin_password.get_secret_value(),
        )
    )
    registered = 0
    for home in plan["homes"]:
        for device in home["devices"]:
            device["password"] = secrets.token_urlsafe(24)
            identity = {"home_id": home["home_id"], "device_id": device["device_id"]}
            try:
                await admin.register_device(**identity, password=device["password"])
            except BrokerAdminError as exc:
                if "already exists" not in str(exc):
                    raise
                # Re-running the plan: rotate the password so the fleet file stays valid.
                await admin.remove_device(device["device_id"])
                await admin.register_device(**identity, password=device["password"])
            registered += 1
    plan["mqtt"] = {
        "host": settings.mqtt_host,
        "port": settings.mqtt_port,
        "ca_file": settings.mqtt_ca_file,
    }
    return registered


def write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(prog="smarthome.devtools.fleet")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="register a simulator fleet plan with the broker")
    reg.add_argument("plan", type=Path)
    reg.add_argument("fleet", type=Path)
    args = parser.parse_args()

    plan: dict[str, Any] = json.loads(args.plan.read_text())
    count = asyncio.run(register(plan, Settings()))
    write_private(args.fleet, plan)
    print(f"registered {count} devices; credentials in {args.fleet} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
