"""Device kinds and what each one exposes in its twin.

Part of the protocol because both sides must agree: a device reports only these
properties, and the hub accepts desired state only for these properties.
"""

from enum import StrEnum


class DeviceKind(StrEnum):
    LIGHT = "light"
    PLUG = "plug"
    THERMOSTAT = "thermostat"
    LOCK = "lock"
    MOTION_SENSOR = "motion_sensor"
    CONTACT_SENSOR = "contact_sensor"
    CLIMATE_SENSOR = "climate_sensor"
    ENERGY_METER = "energy_meter"
    CAMERA = "camera"


STATE_PROPERTIES: dict[DeviceKind, frozenset[str]] = {
    DeviceKind.LIGHT: frozenset({"on", "brightness_pct", "color_temp_k"}),
    DeviceKind.PLUG: frozenset({"on"}),
    DeviceKind.THERMOSTAT: frozenset({"target_temp_c", "hvac_mode"}),
    DeviceKind.LOCK: frozenset({"locked"}),
    DeviceKind.MOTION_SENSOR: frozenset(),
    DeviceKind.CONTACT_SENSOR: frozenset(),
    DeviceKind.CLIMATE_SENSOR: frozenset(),
    DeviceKind.ENERGY_METER: frozenset(),
    DeviceKind.CAMERA: frozenset({"armed"}),
}

# Acting on these changes physical security; commands need recent re-authentication
# and are always audited (phase 7).
CRITICAL_KINDS = frozenset({DeviceKind.LOCK, DeviceKind.CAMERA})
