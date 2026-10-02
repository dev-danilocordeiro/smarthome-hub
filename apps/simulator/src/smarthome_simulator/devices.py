"""Behaviour of one simulated device, independent of MQTT (so it is easy to test).

A device keeps its twin state, produces readings from the shared home environment,
acts on its own the way residents would (lights on at dusk, door unlocked on arrival)
and executes commands at most once per command id.
"""

import math
import random
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from device_protocol import STATE_PROPERTIES, InvalidMessage, MessageKind, decode
from smarthome_simulator.catalog import APPLIANCES, CATALOG, INITIAL_STATE, DeviceType
from smarthome_simulator.environment import HomeEnvironment

REMEMBERED_COMMANDS = 256
LOW_BATTERY_FLOOR = 4.0


@dataclass(frozen=True, slots=True)
class Faults:
    """Per-device fault rates. All zero means a perfectly behaved device."""

    drops_per_hour: float = 0.0
    noise_probability: float = 0.0
    malformed_probability: float = 0.0
    low_battery: bool = False


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    acks: list[dict[str, Any]]
    state_changed: bool


def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_message_id() -> str:
    return secrets.token_urlsafe(12)


@dataclass
class SimulatedDevice:
    device_id: str
    home_id: str
    kind: DeviceType
    room: str
    env: HomeEnvironment
    rng: random.Random
    faults: Faults = field(default_factory=Faults)
    state: dict[str, Any] = field(default_factory=dict)
    battery_pct: float = 100.0
    energy_wh_total: float = 0.0
    rssi_dbm: float = -60.0
    seq: int = 0
    _seen: OrderedDict[str, str] = field(default_factory=OrderedDict)
    _last_occupied: bool | None = None
    _door_open_ticks: int = 0
    _washer_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.state:
            self.state = dict(INITIAL_STATE.get(self.kind, {}))
        if CATALOG[self.kind].battery_powered:
            self.battery_pct = self.rng.uniform(40, 100)

    @property
    def spec(self) -> Any:
        return CATALOG[self.kind]

    # --- Messages -------------------------------------------------------------------

    def presence(self, status: str, reason: str, now: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema_version": "1", "status": status, "reason": reason}
        if now is not None:
            payload["ts"] = iso(now)
            payload["firmware"] = "sim-1.0.0"
        return payload

    def state_message(self, now: datetime) -> dict[str, Any] | None:
        if not self.state:
            return None
        return {
            "schema_version": "1",
            "message_id": new_message_id(),
            "ts": iso(now),
            "reported": dict(self.state),
        }

    def telemetry_message(self, now: datetime, dt_s: float) -> dict[str, Any]:
        sim_now = self.env.sim_time(now)
        readings = self._readings(sim_now, dt_s * self.env.speed)
        if self.rng.random() < self.faults.noise_probability:
            readings = self._spike(readings)
        payload = {
            "schema_version": "1",
            "message_id": new_message_id(),
            "seq": self.seq,
            "ts": iso(now),
            "readings": readings,
        }
        self.seq += 1
        if self.rng.random() < self.faults.malformed_probability:
            # A buggy firmware build: out-of-range value, which the hub must reject.
            payload["readings"] = {"humidity_pct": 180}
        return payload

    # --- Autonomous behaviour ---------------------------------------------------------

    def tick(self, now: datetime) -> bool:
        """Residents interacting with the device. Returns True if the state changed."""
        sim_now = self.env.sim_time(now)
        occupied = self.env.occupied(sim_now)
        arrived = self._last_occupied is False and occupied
        left = self._last_occupied is True and not occupied
        self._last_occupied = occupied
        before = dict(self.state)

        if self.kind is DeviceType.LIGHT:
            late_night = 0.5 <= self.env.hour(sim_now) <= 6
            if occupied and self.env.is_dark(sim_now) and not late_night:
                if not self.state["on"] and self.rng.random() < 0.3:
                    self.state["on"] = True
            elif self.state["on"] and (not occupied or late_night) and self.rng.random() < 0.5:
                self.state["on"] = False
        elif self.kind is DeviceType.LOCK:
            if arrived:
                self.state["locked"] = False
            elif self.room == "entrance" and not self.state["locked"] and self.rng.random() < 0.4:
                self.state["locked"] = True
            if left:
                self.state["locked"] = True
        elif self.kind is DeviceType.CAMERA:
            if left:
                self.state["armed"] = True
            elif arrived:
                self.state["armed"] = False
        elif (
            self.kind is DeviceType.CONTACT_SENSOR and (arrived or left) and self.room == "entrance"
        ):
            self._door_open_ticks = 2

        return self.state != before

    # --- Commands -------------------------------------------------------------------

    def handle_command(self, raw: bytes, now: datetime) -> CommandOutcome:
        try:
            command = decode(MessageKind.COMMAND, raw)
        except InvalidMessage:
            return CommandOutcome(acks=[], state_changed=False)

        command_id: str = command["command_id"]
        if command_id in self._seen:
            # QoS 1 redelivery: report the original outcome, never execute twice.
            return CommandOutcome([self._ack(command_id, self._seen[command_id], now)], False)

        acks = [self._ack(command_id, "received", now)]
        changed = False
        expires_at = datetime.fromisoformat(command["expires_at"])
        if expires_at <= now:
            status, reason = "expired", "received after expires_at"
        elif command["action"] == "set_state":
            desired: dict[str, Any] = command["desired"]
            unsupported = set(desired) - STATE_PROPERTIES[self.kind]
            if unsupported:
                status, reason = "rejected", f"unsupported properties: {sorted(unsupported)}"
            else:
                before = dict(self.state)
                self.state.update(desired)
                changed = self.state != before
                status, reason = "applied", ""
        elif command["action"] == "reboot":
            self.seq = 0
            status, reason = "applied", ""
        else:  # identify: blink an LED
            status, reason = "applied", ""

        self._remember(command_id, status)
        acks.append(self._ack(command_id, status, now, reason))
        return CommandOutcome(acks=acks, state_changed=changed)

    def _ack(self, command_id: str, status: str, now: datetime, reason: str = "") -> dict[str, Any]:
        ack: dict[str, Any] = {
            "schema_version": "1",
            "command_id": command_id,
            "ts": iso(now),
            "status": status,
        }
        if reason:
            ack["reason"] = reason
        return ack

    def _remember(self, command_id: str, status: str) -> None:
        self._seen[command_id] = status
        while len(self._seen) > REMEMBERED_COMMANDS:
            self._seen.popitem(last=False)

    # --- Readings -------------------------------------------------------------------

    def _readings(self, sim_now: datetime, sim_dt_s: float) -> dict[str, Any]:
        self.rssi_dbm = max(-95.0, min(-35.0, self.rssi_dbm + self.rng.gauss(0, 1.5)))
        readings: dict[str, Any] = {"rssi_dbm": round(self.rssi_dbm, 1)}
        if self.spec.battery_powered:
            drain = 0.8 if self.faults.low_battery else 0.002
            self.battery_pct = max(LOW_BATTERY_FLOOR, self.battery_pct - drain)
            readings["battery_pct"] = round(self.battery_pct, 1)

        occupied = self.env.occupied(sim_now)
        match self.kind:
            case DeviceType.LIGHT:
                power = 9.0 * self.state["brightness_pct"] / 100 if self.state["on"] else 0.4
                readings["power_w"] = self._report_power(power)
            case DeviceType.PLUG:
                power = self._appliance_power(sim_now, occupied) if self.state["on"] else 0.0
                readings["power_w"] = self._report_power(power)
                self.energy_wh_total += power * sim_dt_s / 3600
                readings["energy_wh_total"] = round(self.energy_wh_total, 3)
                readings["voltage_v"] = round(self.rng.gauss(127, 1.2), 1)
            case DeviceType.THERMOSTAT:
                target = float(self.state["target_temp_c"])
                hvac_on = (
                    self.state["hvac_mode"] != "off" and abs(self.env.indoor_temp_c - target) > 0.5
                )
                self.env.power_w[f"{self.device_id}:hvac"] = 1500.0 if hvac_on else 0.0
                temp = self.env.step_indoor_temp(
                    sim_now, target_c=target, hvac_on=hvac_on, dt_s=sim_dt_s
                )
                readings["temperature_c"] = round(temp + self.rng.gauss(0, 0.1), 2)
                readings["humidity_pct"] = self._humidity(sim_now)
            case DeviceType.CLIMATE_SENSOR:
                offset = {"bedroom": -0.8, "bathroom": 1.0}.get(self.room, 0.0)
                temp = self.env.indoor_temp_c + offset + self.rng.gauss(0, 0.15)
                readings["temperature_c"] = round(temp, 2)
                readings["humidity_pct"] = self._humidity(sim_now)
            case DeviceType.MOTION_SENSOR:
                p = 0.35 if occupied else 0.01  # pets and false positives
                readings["motion"] = self.rng.random() < p
                readings["illuminance_lux"] = round(self.env.daylight_lux(sim_now), 1)
            case DeviceType.CAMERA:
                p = 0.25 if occupied else 0.02
                readings["motion"] = self.rng.random() < p
            case DeviceType.CONTACT_SENSOR:
                if self._door_open_ticks > 0:
                    self._door_open_ticks -= 1
                    readings["contact_open"] = True
                else:
                    window = self.room != "entrance"
                    daytime = 9 <= self.env.hour(sim_now) <= 18
                    readings["contact_open"] = window and daytime and self.rng.random() < 0.1
            case DeviceType.ENERGY_METER:
                total = self.env.base_load_w + sum(self.env.power_w.values())
                total *= self.rng.uniform(0.98, 1.02)
                self.energy_wh_total += total * sim_dt_s / 3600
                readings["power_w"] = round(total, 1)
                readings["energy_wh_total"] = round(self.energy_wh_total, 3)
                readings["voltage_v"] = round(self.rng.gauss(127, 1.5), 1)
        return readings

    def _report_power(self, watts: float) -> float:
        self.env.power_w[self.device_id] = watts
        return round(watts * self.rng.uniform(0.97, 1.03), 2)

    def _humidity(self, sim_now: datetime) -> float:
        h = 55 + 15 * math.sin(2 * math.pi * (self.env.hour(sim_now) - 3) / 24)
        return round(min(100.0, max(0.0, h + self.rng.gauss(0, 1.5))), 1)

    def _appliance_power(self, sim_now: datetime, occupied: bool) -> float:
        appliance = APPLIANCES.get(self.room, "lamp")
        hour = self.env.hour(sim_now)
        match appliance:
            case "fridge":
                # Compressor duty cycle: ~12 minutes on every 30.
                return self.rng.uniform(110, 140) if (sim_now.minute % 30) < 12 else 3.0
            case "tv":
                return self.rng.uniform(80, 110) if occupied and 19 <= hour <= 23 else 1.0
            case "computer":
                working = sim_now.weekday() < 5 and 9 <= hour <= 18
                return self.rng.uniform(60, 160) if working or (occupied and hour >= 20) else 4.0
            case "washer":
                if self._washer_until is None and self.rng.random() < 0.002:
                    self._washer_until = sim_now + timedelta(minutes=90)
                if self._washer_until and sim_now < self._washer_until:
                    heating = (self._washer_until - sim_now) > timedelta(minutes=60)
                    return self.rng.uniform(1800, 2100) if heating else self.rng.uniform(250, 450)
                self._washer_until = None
                return 0.5
            case _:
                return 7.0 if occupied and self.env.is_dark(sim_now) else 0.3

    def _spike(self, readings: dict[str, Any]) -> dict[str, Any]:
        """A noisy sample: plausible enough to pass the schema, wrong enough to notice."""
        spiked = dict(readings)
        if "temperature_c" in spiked:
            spiked["temperature_c"] = round(spiked["temperature_c"] + self.rng.choice([-8, 8]), 2)
        if "power_w" in spiked:
            spiked["power_w"] = round(spiked["power_w"] * self.rng.uniform(3, 6), 2)
        return spiked
