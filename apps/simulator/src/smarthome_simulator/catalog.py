"""What each simulated device type is, reports and accepts."""

from dataclasses import dataclass

from device_protocol import DeviceKind

# The kinds themselves are part of the protocol; the simulator only adds behaviour.
DeviceType = DeviceKind


@dataclass(frozen=True, slots=True)
class Spec:
    prefix: str
    telemetry_every_s: float
    battery_powered: bool


CATALOG: dict[DeviceType, Spec] = {
    DeviceType.LIGHT: Spec("light", 60, False),
    DeviceType.PLUG: Spec("plug", 10, False),
    DeviceType.THERMOSTAT: Spec("thermo", 30, False),
    DeviceType.LOCK: Spec("lock", 120, True),
    DeviceType.MOTION_SENSOR: Spec("motion", 30, True),
    DeviceType.CONTACT_SENSOR: Spec("contact", 30, True),
    DeviceType.CLIMATE_SENSOR: Spec("climate", 60, True),
    DeviceType.ENERGY_METER: Spec("meter", 5, False),
    DeviceType.CAMERA: Spec("camera", 30, False),
}

INITIAL_STATE: dict[DeviceType, dict[str, object]] = {
    DeviceType.LIGHT: {"on": False, "brightness_pct": 80, "color_temp_k": 2700},
    DeviceType.PLUG: {"on": True},
    DeviceType.THERMOSTAT: {"target_temp_c": 21.0, "hvac_mode": "auto"},
    DeviceType.LOCK: {"locked": True},
    DeviceType.CAMERA: {"armed": False},
}

# What sits behind each smart plug, by room. Drives realistic consumption curves.
APPLIANCES: dict[str, str] = {
    "living_room": "tv",
    "kitchen": "fridge",
    "office": "computer",
    "utility": "washer",
}
