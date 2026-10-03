"""What raises and clears alerts, and when a notification may be emailed. Pure functions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from device_protocol import DeviceKind
from smarthome.modules.notifications.domain.model import (
    AlertKind,
    Clear,
    Preferences,
    Raise,
    Severity,
    Timing,
)
from smarthome.shared.events import DeviceEvent, DeviceEventKind

# Losing these is a security problem, not an inconvenience.
CRITICAL_KINDS = frozenset({DeviceKind.LOCK, DeviceKind.CAMERA})


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    id: str
    name: str | None
    kind: DeviceKind

    @property
    def label(self) -> str:
        return self.name or self.id


def offline_key(device_id: str) -> str:
    return f"{AlertKind.DEVICE_OFFLINE}:{device_id}"


def battery_key(device_id: str) -> str:
    return f"{AlertKind.LOW_BATTERY}:{device_id}"


def budget_key(month_start: datetime, threshold: int) -> str:
    return f"{AlertKind.ENERGY_BUDGET}:{month_start:%Y-%m}:{threshold}"


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def on_device_event(event: DeviceEvent, device: DeviceInfo, timing: Timing) -> list[Raise | Clear]:
    severity = Severity.CRITICAL if device.kind in CRITICAL_KINDS else Severity.WARNING
    match event.kind:
        case DeviceEventKind.PRESENCE if event.data.get("online") is False:
            return [
                Raise(
                    key=offline_key(device.id),
                    kind=AlertKind.DEVICE_OFFLINE,
                    severity=severity,
                    device_id=device.id,
                    title=f"{device.label} is offline",
                    observed_at=event.at,
                    due_at=event.at + timing.offline_grace,
                )
            ]
        case DeviceEventKind.PRESENCE if event.data.get("online") is True:
            return [Clear(offline_key(device.id), event.at)]
        case DeviceEventKind.TELEMETRY if _number(event.data.get("battery_pct")):
            pct = float(event.data["battery_pct"])
            if pct < timing.battery_low_pct:
                return [
                    Raise(
                        key=battery_key(device.id),
                        kind=AlertKind.LOW_BATTERY,
                        severity=severity,
                        device_id=device.id,
                        title=f"{device.label} battery is low ({pct:.0f}%)",
                        observed_at=event.at,
                        details={"battery_pct": pct},
                    )
                ]
            if pct >= timing.battery_ok_pct:
                return [Clear(battery_key(device.id), event.at)]
    return []


def clears(event: DeviceEvent, timing: Timing) -> list[Clear]:
    """The clears `on_device_event` would return, decided without knowing the device.
    Most readings are of this kind (a healthy battery, a device coming back), so the
    engine handles them without looking the device up."""
    match event.kind:
        case DeviceEventKind.PRESENCE if event.data.get("online") is True:
            return [Clear(offline_key(event.device_id), event.at)]
        case DeviceEventKind.TELEMETRY if _number(event.data.get("battery_pct")):
            if float(event.data["battery_pct"]) >= timing.battery_ok_pct:
                return [Clear(battery_key(event.device_id), event.at)]
    return []


def on_budget(
    *, month_start: datetime, used_kwh: float, budget_kwh: float, now: datetime, timing: Timing
) -> list[Raise]:
    percent = 100 * used_kwh / budget_kwh
    return [
        Raise(
            key=budget_key(month_start, threshold),
            kind=AlertKind.ENERGY_BUDGET,
            severity=Severity.WARNING if threshold >= 100 else Severity.INFO,  # noqa: PLR2004
            device_id=None,
            title=f"Energy use reached {threshold}% of this month's budget",
            observed_at=now,
            details={
                "month": f"{month_start:%Y-%m}",
                "used_kwh": round(used_kwh, 1),
                "budget_kwh": round(budget_kwh, 1),
                "percent": round(percent),
            },
        )
        for threshold in timing.budget_thresholds
        if percent >= threshold
    ]


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight


def email_at(
    prefs: Preferences, severity: Severity, *, now: datetime, tz: ZoneInfo
) -> datetime | None:
    """When to email someone about a new alert: now, after their quiet hours end, or
    never (email off, or below the severity they asked for). Critical alerts ignore
    quiet hours: a lock going offline at 3 a.m. is exactly when to wake someone."""
    if not prefs.email_enabled or severity < prefs.min_email_severity:
        return None
    if prefs.quiet_start is None or prefs.quiet_end is None or severity >= Severity.CRITICAL:
        return now
    local = now.astimezone(tz)
    if not in_quiet_hours(local.hour, prefs.quiet_start, prefs.quiet_end):
        return now
    wake = local.replace(hour=prefs.quiet_end, minute=0, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    return wake.astimezone(UTC)
