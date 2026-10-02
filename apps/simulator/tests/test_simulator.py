import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from device_protocol import InvalidMessage, MessageKind, topic, validate
from smarthome_simulator.catalog import CATALOG, DeviceType
from smarthome_simulator.devices import Faults, SimulatedDevice
from smarthome_simulator.environment import HomeEnvironment
from smarthome_simulator.plan import build_plan

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # a Monday


def device(
    kind: DeviceType, *, faults: Faults | None = None, room: str = "living_room"
) -> SimulatedDevice:
    env = HomeEnvironment(rng=random.Random(1), started_at=T0)  # noqa: S311
    return SimulatedDevice(
        device_id=f"{CATALOG[kind].prefix}-0001",
        home_id="home-1",
        kind=kind,
        room=room,
        env=env,
        rng=random.Random(2),  # noqa: S311
        faults=faults or Faults(),
    )


def command(**overrides: Any) -> bytes:
    payload = {
        "schema_version": "1",
        "command_id": str(uuid.uuid4()),
        "issued_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(seconds=30)).isoformat(),
        "action": "set_state",
        "desired": {"on": True, "brightness_pct": 30},
    } | overrides
    return json.dumps({k: v for k, v in payload.items() if v is not None}).encode()


def test_a_plan_is_reproducible_from_its_seed_and_ids_are_valid_topic_segments() -> None:
    first = build_plan(homes=3, devices_per_home=20, seed=7)
    second = build_plan(homes=3, devices_per_home=20, seed=7)

    assert first == second
    ids = [d["device_id"] for h in first["homes"] for d in h["devices"]]
    assert len(ids) == len(set(ids)) == 60
    for home in first["homes"]:
        for d in home["devices"]:
            topic(home["home_id"], d["device_id"], MessageKind.TELEMETRY)  # raises if invalid


def test_a_different_seed_gives_a_different_fleet() -> None:
    assert build_plan(homes=1, devices_per_home=5, seed=1) != build_plan(
        homes=1, devices_per_home=5, seed=2
    )


@pytest.mark.parametrize("kind", list(DeviceType))
def test_every_device_type_only_ever_emits_schema_valid_messages(kind: DeviceType) -> None:
    sim = device(kind, faults=Faults(noise_probability=0.3))
    now = T0
    for _ in range(300):  # a simulated day at speed 1 with 5-minute steps
        now += timedelta(minutes=5)
        sim.tick(now)
        validate(MessageKind.TELEMETRY, sim.telemetry_message(now, 300))
        if state := sim.state_message(now):
            validate(MessageKind.STATE, state)
    validate(MessageKind.PRESENCE, sim.presence("online", "boot", now))
    validate(MessageKind.PRESENCE, sim.presence("offline", "connection_lost"))


def test_the_malformed_payload_fault_produces_messages_the_hub_must_reject() -> None:
    sim = device(DeviceType.CLIMATE_SENSOR, faults=Faults(malformed_probability=1.0))

    with pytest.raises(InvalidMessage):
        validate(MessageKind.TELEMETRY, sim.telemetry_message(T0, 60))


def test_a_redelivered_command_is_acknowledged_again_but_executed_once() -> None:
    sim = device(DeviceType.LIGHT)
    raw = command()

    first = sim.handle_command(raw, T0)
    sim.state["brightness_pct"] = 90  # someone changes it locally afterwards
    second = sim.handle_command(raw, T0 + timedelta(seconds=1))

    assert [a["status"] for a in first.acks] == ["received", "applied"]
    assert first.state_changed
    assert [a["status"] for a in second.acks] == ["applied"]
    assert not second.state_changed
    assert sim.state["brightness_pct"] == 90
    for ack in first.acks + second.acks:
        validate(MessageKind.COMMAND_ACK, ack)


def test_a_command_arriving_after_it_expired_is_not_executed() -> None:
    sim = device(DeviceType.LOCK)
    raw = command(desired={"locked": False}, expires_at=(T0 - timedelta(seconds=1)).isoformat())

    outcome = sim.handle_command(raw, T0)

    assert outcome.acks[-1]["status"] == "expired"
    assert sim.state["locked"] is True


def test_a_device_rejects_properties_it_does_not_have() -> None:
    sim = device(DeviceType.PLUG)

    outcome = sim.handle_command(command(desired={"brightness_pct": 10}), T0)

    assert outcome.acks[-1]["status"] == "rejected"
    assert "brightness_pct" in outcome.acks[-1]["reason"]


def test_an_older_set_state_arriving_late_does_not_undo_a_newer_one() -> None:
    sim = device(DeviceType.LIGHT)
    newer = command(issued_at=(T0 + timedelta(seconds=2)).isoformat(), desired={"on": False})
    older = command(issued_at=T0.isoformat(), desired={"on": True})

    sim.handle_command(newer, T0 + timedelta(seconds=3))
    outcome = sim.handle_command(older, T0 + timedelta(seconds=3))

    assert outcome.acks[-1]["status"] == "rejected"
    assert outcome.acks[-1]["reason"] == "superseded by a newer command"
    assert sim.state["on"] is False


def test_garbage_on_the_command_topic_is_ignored() -> None:
    assert device(DeviceType.LIGHT).handle_command(b"{not json", T0).acks == []


def test_weekday_occupancy_follows_working_hours() -> None:
    env = HomeEnvironment(rng=random.Random(0), started_at=T0)  # noqa: S311
    monday = datetime(2026, 10, 5, tzinfo=UTC)

    assert env.occupied(monday.replace(hour=7))
    assert not env.occupied(monday.replace(hour=11))
    assert env.occupied(monday.replace(hour=21))


def test_outdoor_temperature_peaks_in_the_afternoon_and_bottoms_out_before_dawn() -> None:
    env = HomeEnvironment(rng=random.Random(0), started_at=T0)  # noqa: S311
    day = datetime(2026, 10, 5, tzinfo=UTC)
    temps = {h: env.outdoor_temp_c(day.replace(hour=h)) for h in range(24)}

    assert max(temps, key=lambda h: temps[h]) == 15
    assert min(temps, key=lambda h: temps[h]) == 3


def test_the_energy_meter_reflects_what_the_rest_of_the_home_draws() -> None:
    meter = device(DeviceType.ENERGY_METER)
    idle = meter.telemetry_message(T0, 5)["readings"]["power_w"]
    meter.env.power_w["washer"] = 2000.0

    busy = meter.telemetry_message(T0, 5)["readings"]["power_w"]

    assert busy - idle > 1800
