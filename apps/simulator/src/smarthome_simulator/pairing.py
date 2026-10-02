"""Device-side pairing: trade each pairing code for an identity and broker credentials,
over the hub's public provisioning endpoint, the same call real firmware makes."""

import asyncio
import json
import logging
from typing import Any

import httpx

FIRMWARE = "sim-1.0.0"
MAX_ATTEMPTS = 5

log = logging.getLogger("smarthome_simulator")


async def claim_one(http: httpx.AsyncClient, api_url: str, code: str, kind: str) -> dict[str, Any]:
    for _ in range(MAX_ATTEMPTS):
        response = await http.post(
            f"{api_url}/provisioning/claim",
            json={"code": code, "kind": kind, "firmware": FIRMWARE},
        )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            await asyncio.sleep(float(response.headers.get("retry-after", "5")))
            continue
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
    raise RuntimeError(f"pairing kept being rate limited for code {code}")


async def claim_fleet(pairing: dict[str, Any], *, api_url: str, ca_file: str) -> dict[str, Any]:
    """Returns a fleet file for `run`: per-device ids and credentials from the hub."""
    homes: list[dict[str, Any]] = []
    broker: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=15) as http:
        for home in pairing["homes"]:
            devices = []
            for planned in home["devices"]:
                claimed = await claim_one(http, api_url, planned["code"], planned["type"])
                broker = {
                    "host": claimed["mqtt"]["host"],
                    "port": claimed["mqtt"]["port"],
                    "ca_file": ca_file,
                }
                devices.append(
                    {
                        "device_id": claimed["device_id"],
                        "type": planned["type"],
                        "room": planned["room"],
                        "password": claimed["mqtt"]["password"],
                    }
                )
            homes.append({"home_id": home["home_id"], "name": home["name"], "devices": devices})
            log.info(
                json.dumps(
                    {"event": "home_paired", "home_id": home["home_id"], "devices": len(devices)}
                )
            )
    return {"seed": pairing.get("seed", 0), "mqtt": broker, "homes": homes}
