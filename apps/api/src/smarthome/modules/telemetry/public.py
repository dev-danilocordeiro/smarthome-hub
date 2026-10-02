"""Public interface of the `telemetry` module (energy and automations read through this)."""

from smarthome.modules.telemetry.application.services import TelemetryQueries
from smarthome.modules.telemetry.domain.model import Point, Resolution

__all__ = ["Point", "Resolution", "TelemetryQueries"]
