"""Fleet plans: which homes exist and which devices sit in which room.

A plan has no secrets. The hub side (`smarthome.devtools.fleet register`, later real
pairing) adds credentials to produce the fleet file the simulator runs.
"""

import random
import uuid
from typing import Any

from smarthome_simulator.catalog import CATALOG, DeviceType

# Ordered by how essential a device is to a believable home; a plan with fewer devices
# per home keeps the top of the list.
LAYOUT: list[tuple[str, DeviceType]] = [
    ("entrance", DeviceType.LOCK),
    ("entrance", DeviceType.CONTACT_SENSOR),
    ("utility", DeviceType.ENERGY_METER),
    ("hallway", DeviceType.THERMOSTAT),
    ("living_room", DeviceType.LIGHT),
    ("living_room", DeviceType.MOTION_SENSOR),
    ("living_room", DeviceType.CLIMATE_SENSOR),
    ("living_room", DeviceType.PLUG),
    ("kitchen", DeviceType.LIGHT),
    ("kitchen", DeviceType.PLUG),
    ("bedroom", DeviceType.LIGHT),
    ("bedroom", DeviceType.CLIMATE_SENSOR),
    ("bedroom", DeviceType.CONTACT_SENSOR),
    ("entrance", DeviceType.CAMERA),
    ("entrance", DeviceType.LIGHT),
    ("bathroom", DeviceType.LIGHT),
    ("bathroom", DeviceType.MOTION_SENSOR),
    ("office", DeviceType.LIGHT),
    ("office", DeviceType.PLUG),
    ("utility", DeviceType.PLUG),
    ("living_room", DeviceType.LIGHT),
    ("bedroom", DeviceType.LIGHT),
    ("entrance", DeviceType.MOTION_SENSOR),
    ("kitchen", DeviceType.CONTACT_SENSOR),
]

HOME_NAMES = ["Casa Centro", "Apartamento Praia", "Sítio Serra", "Casa Lago", "Loft Vila"]


def build_plan(*, homes: int, devices_per_home: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    plan_homes = []
    for index in range(homes):
        home_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        devices = []
        for room, kind in _layout(devices_per_home):
            devices.append(
                {
                    "device_id": f"{CATALOG[kind].prefix}-{rng.getrandbits(32):08x}",
                    "type": kind.value,
                    "room": room,
                }
            )
        name = HOME_NAMES[index % len(HOME_NAMES)]
        plan_homes.append({"home_id": home_id, "name": f"{name} {index + 1}", "devices": devices})
    return {"seed": seed, "homes": plan_homes}


def _layout(count: int) -> list[tuple[str, DeviceType]]:
    """The first `count` layout entries, cycling the list for very large homes."""
    return [LAYOUT[i % len(LAYOUT)] for i in range(count)]
