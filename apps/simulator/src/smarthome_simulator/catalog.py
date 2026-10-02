"""What each simulated device type is, reports and accepts."""

from dataclasses import dataclass
from enum import StrEnum


class DeviceType(StrEnum):
    LIGHT = "light"
    PLUG = "plug"
    THERMOSTAT = "thermostat"
    LOCK = "lock"
    MOTION_SENSOR = "motion_sensor"
    CONTACT_SENSOR = "contact_sensor"
    CLIMATE_SENSOR = "climate_sensor"
    ENERGY_METER = "energy_meter"
    CAMERA = "camera"


@dataclass(frozen=True, slots=True)
class Spec:
    prefix: str
    telemetry_every_s: float
    battery_powered: bool
    # Twin properties the device accepts in `set_state` and reports in `state`.
    state_properties: frozenset[str]


CATALOG: dict[DeviceType, Spec] = {
    DeviceType.LIGHT: Spec("light", 60, False, frozenset({"on", "brightness_pct", "color_temp_k"})),
    DeviceType.PLUG: Spec("plug", 10, False, frozenset({"on"})),
    DeviceType.THERMOSTAT: Spec("thermo", 30, False, frozenset({"target_temp_c", "hvac_mode"})),
    DeviceType.LOCK: Spec("lock", 120, True, frozenset({"locked"})),
    DeviceType.MOTION_SENSOR: Spec("motion", 30, True, frozenset()),
    DeviceType.CONTACT_SENSOR: Spec("contact", 30, True, frozenset()),
    DeviceType.CLIMATE_SENSOR: Spec("climate", 60, True, frozenset()),
    DeviceType.ENERGY_METER: Spec("meter", 5, False, frozenset()),
    DeviceType.CAMERA: Spec("camera", 30, False, frozenset({"armed"})),
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
