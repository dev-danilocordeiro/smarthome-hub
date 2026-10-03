"""The automations application layer against in-memory ports: managing automations and
scenes, and the engine turning events, timers and schedules into runs."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from smarthome.modules.automations.domain.definition import Source, SourceKind
from smarthome.modules.automations.domain.engine import Limits, Observation, TriggerState
from smarthome.modules.automations.domain.errors import (
    AutomationNotFound,
    InvalidDefinition,
    NameTaken,
    SceneInUse,
    VersionMismatch,
)
from smarthome.modules.automations.domain.model import (
    Automation,
    AutomationStatus,
    RunStatus,
    SceneState,
)
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from tests.unit.automations.fakes import HOME, NOW, Harness, harness

ZONE = "America/Sao_Paulo"
MOTION = Source(SourceKind.TELEMETRY, "motion-1", "motion")
LIGHT_ON = Source(SourceKind.DEVICE_STATE, "light-1", "on")


def motion_turns_on_light(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "triggers": [
            {
                "type": "telemetry",
                "device_id": "motion-1",
                "metric": "motion",
                "op": "eq",
                "value": True,
            }
        ],
        "actions": [
            {
                "type": "command",
                "device_id": "light-1",
                "action": "set_state",
                "desired": {"on": True},
            }
        ],
    } | overrides


async def save(h: Harness, name: str = "Hall light", **overrides: Any) -> Automation:
    saved = await h.service.create(
        home_id=HOME,
        timezone=ZONE,
        name=name,
        description=None,
        enabled=True,
        definition=motion_turns_on_light(**overrides),
        actor="alice",
    )
    return saved.automation


def motion(value: bool, seconds: float = 0, device: str = "motion-1") -> DeviceEvent:
    return DeviceEvent(
        HOME,
        device,
        DeviceEventKind.TELEMETRY,
        NOW + timedelta(seconds=seconds),
        {"motion": value},
    )


async def prime_motion_off(h: Harness) -> None:
    h.devices.values[MOTION] = Observation(False, NOW - timedelta(minutes=1))


# --- Managing ---------------------------------------------------------------------------


async def test_saving_records_a_revision_primes_triggers_and_audits() -> None:
    h = harness()
    await prime_motion_off(h)

    automation = await save(h)

    assert automation.version == 1
    assert [r.change for r in h.store.revisions] == ["created"]
    assert h.store.states[(automation.id, 0)] == TriggerState(
        False, NOW - timedelta(minutes=1), None
    )
    assert [e.action for e in h.store.audit] == ["automation.created"]
    assert h.directory.invalidated == [HOME]


async def test_a_definition_referencing_unknown_devices_is_rejected_before_saving() -> None:
    h = harness()

    with pytest.raises(InvalidDefinition, match="no device 'light-9'"):
        await save(
            h,
            actions=[{"type": "command", "device_id": "light-9", "action": "identify"}],
        )
    assert h.store.automations == {}


async def test_names_are_unique_per_home() -> None:
    h = harness()
    await save(h, "Hall light")

    with pytest.raises(NameTaken):
        await save(h, "hall LIGHT")


async def test_an_edit_must_name_the_current_version() -> None:
    h = harness()
    automation = await save(h)
    edit: dict[str, Any] = {
        "home_id": HOME,
        "automation_id": automation.id,
        "name": "Hall light (night)",
        "description": None,
        "enabled": True,
        "definition": motion_turns_on_light(cooldown_s=30),
        "actor": "bob",
    }

    saved = await h.service.update(expected_version=1, **edit)
    with pytest.raises(VersionMismatch):
        await h.service.update(expected_version=1, **edit)

    assert saved.automation.version == 2
    assert [(r.version, r.change) for r in await h.service.revisions(HOME, automation.id)] == [
        (2, "updated"),
        (1, "created"),
    ]


async def test_another_homes_automation_is_not_found() -> None:
    h = harness()
    automation = await save(h)

    with pytest.raises(AutomationNotFound):
        await h.service.get(uuid4(), automation.id)


async def test_saving_warns_about_possible_loops() -> None:
    h = harness()
    await save(
        h,
        "On",
        triggers=[
            {
                "type": "device_state",
                "device_id": "light-1",
                "property": "on",
                "op": "eq",
                "value": False,
            }
        ],
    )

    saved = await h.service.create(
        home_id=HOME,
        timezone=ZONE,
        name="Off",
        description=None,
        enabled=True,
        definition=motion_turns_on_light(
            triggers=[
                {
                    "type": "device_state",
                    "device_id": "light-1",
                    "property": "on",
                    "op": "eq",
                    "value": True,
                }
            ],
            actions=[
                {
                    "type": "command",
                    "device_id": "light-1",
                    "action": "set_state",
                    "desired": {"on": False},
                }
            ],
        ),
        actor="alice",
    )

    assert saved.warnings == ["'Off', 'On' can trigger each other in a loop"]


async def test_targets_include_the_devices_of_referenced_scenes() -> None:
    h = harness()
    scene = await h.service.create_scene(
        home_id=HOME,
        name="Away",
        states=[SceneState("lock-1", {"locked": True}), SceneState("plug-1", {"on": False})],
        actor="alice",
    )

    targets = await h.service.targets(
        HOME, motion_turns_on_light(actions=[{"type": "scene", "scene_id": str(scene.id)}])
    )

    assert sorted(t.device_id for t in targets) == ["lock-1", "plug-1"]


async def test_a_scene_used_by_an_automation_cannot_be_deleted() -> None:
    h = harness()
    scene = await h.service.create_scene(
        home_id=HOME, name="Away", states=[SceneState("plug-1", {"on": False})], actor="alice"
    )
    await save(h, "Leave", actions=[{"type": "scene", "scene_id": str(scene.id)}])

    with pytest.raises(SceneInUse, match="'Leave'"):
        await h.service.delete_scene(
            home_id=HOME, scene_id=scene.id, expected_version=None, actor="alice"
        )


async def test_activating_a_scene_sends_one_command_per_device_and_audits() -> None:
    h = harness()
    h.commands.refuse.add("plug-1")
    scene = await h.service.create_scene(
        home_id=HOME,
        name="Movie",
        states=[
            SceneState("light-1", {"on": True, "brightness_pct": 10}),
            SceneState("plug-1", {"on": False}),
        ],
        actor="alice",
    )

    outcomes = await h.service.activate_scene(home_id=HOME, scene_id=scene.id, actor="bob")

    assert [o.device_id for o in outcomes if o.command_id] == ["light-1"]
    assert [o.device_id for o in outcomes if o.error] == ["plug-1"]
    assert h.commands.issued[0]["actor"] == "bob"
    assert h.store.audit[-1].action == "scene.activated"
    assert h.store.audit[-1].details["failed"] == ["plug-1"]


async def test_disabling_drops_schedules_and_enabling_restores_them() -> None:
    h = harness()
    automation = await save(h, triggers=[{"type": "schedule", "at": "22:00"}])
    assert (automation.id, 0) in h.store.schedules

    await h.service.set_enabled(
        home_id=HOME, automation_id=automation.id, enabled=False, actor="alice"
    )
    assert h.store.schedules == {}

    await h.service.set_enabled(
        home_id=HOME, automation_id=automation.id, enabled=True, actor="alice"
    )
    assert (automation.id, 0) in h.store.schedules


# --- Engine: events ---------------------------------------------------------------------


async def test_an_event_that_makes_a_trigger_match_runs_the_actions_once() -> None:
    h = harness()
    await prime_motion_off(h)
    automation = await save(h)

    started = await h.engine.handle_event("1-0", motion(True))
    again = await h.engine.handle_event("1-0", motion(True))  # redelivered

    assert (started, again) == (1, 0)
    assert h.commands.issued == [
        {
            "device_id": "light-1",
            "action": "set_state",
            "desired": {"on": True},
            "actor": f"automation:{automation.id}",
        }
    ]
    [run] = h.store.runs.values()
    assert run.status is RunStatus.COMPLETED
    assert run.event_key == "event:1-0"


async def test_events_of_other_devices_or_metrics_are_ignored() -> None:
    h = harness()
    await prime_motion_off(h)
    await save(h)

    assert await h.engine.handle_event("1-0", motion(True, device="motion-2")) == 0
    battery = DeviceEvent(HOME, "motion-1", DeviceEventKind.TELEMETRY, NOW, {"battery_pct": 9})
    assert await h.engine.handle_event("2-0", battery) == 0
    assert h.store.runs == {}


async def test_a_failing_condition_records_a_skipped_run_and_sends_nothing() -> None:
    h = harness()
    await prime_motion_off(h)
    h.devices.values[LIGHT_ON] = Observation(True, NOW)
    await save(
        h,
        conditions=[
            {
                "type": "device_state",
                "device_id": "light-1",
                "property": "on",
                "op": "eq",
                "value": False,
            }
        ],
    )

    await h.engine.handle_event("1-0", motion(True))

    [run] = h.store.runs.values()
    assert run.status is RunStatus.SKIPPED
    assert run.reason == "condition 0 failed"
    assert h.commands.issued == []


async def test_cooldown_suppresses_runs_without_suspending() -> None:
    h = harness()
    await prime_motion_off(h)
    automation = await save(h, cooldown_s=300)

    await h.engine.handle_event("1-0", motion(True, 0))
    await h.engine.handle_event("2-0", motion(False, 1))
    h.clock.advance(seconds=2)
    await h.engine.handle_event("3-0", motion(True, 2))

    statuses = sorted(r.status for r in h.store.runs.values())
    assert statuses == [RunStatus.COMPLETED, RunStatus.SUPPRESSED]
    assert h.store.automations[automation.id].armed


async def test_a_long_causal_chain_suspends_the_automation() -> None:
    h = harness(Limits(max_chain_depth=2))
    await prime_motion_off(h)
    automation = await save(h)
    h.causality.depths["motion-1"] = 3  # this change was caused by a chain of 3 runs

    await h.engine.handle_event("1-0", motion(True))

    [run] = h.store.runs.values()
    assert run.status is RunStatus.SUPPRESSED
    suspended = h.store.automations[automation.id]
    assert suspended.status is AutomationStatus.SUSPENDED
    assert suspended.status_reason is not None
    assert "chain of 3" in suspended.status_reason
    assert h.store.audit[-1].action == "automation.suspended"
    assert h.store.audit[-1].actor == "system:automations"
    assert h.commands.issued == []
    assert h.directory.invalidated[-1] == HOME
    # Suspended automations stop listening.
    assert await h.engine.handle_event("2-0", motion(False, 1)) == 0


async def test_too_many_runs_in_a_minute_suspends_the_automation() -> None:
    h = harness(Limits(max_runs_per_minute=3))
    await prime_motion_off(h)
    automation = await save(h)

    for i in range(10):
        h.clock.advance(seconds=1)
        await h.engine.handle_event(f"{i}-1", motion(True, 2 * i + 1))
        await h.engine.handle_event(f"{i}-2", motion(False, 2 * i + 2))

    assert len(h.commands.issued) == 3
    assert h.store.automations[automation.id].status is AutomationStatus.SUSPENDED


async def test_commands_from_a_run_mark_their_devices_one_level_deeper() -> None:
    h = harness()
    await prime_motion_off(h)
    h.causality.depths["motion-1"] = 1
    await save(h)

    await h.engine.handle_event("1-0", motion(True))

    # Marked before the command went out, so the device's reply is attributed.
    assert h.commands.depth_seen == [2]
    assert h.causality.depths["light-1"] == 2


async def test_an_edit_resets_the_trigger_so_old_observations_do_not_fire_the_new_version() -> None:
    h = harness()
    await prime_motion_off(h)
    automation = await save(h)
    await h.engine.handle_event("1-0", motion(False, 5))
    await h.service.update(
        home_id=HOME,
        automation_id=automation.id,
        expected_version=1,
        name="Hall light",
        description=None,
        enabled=True,
        definition=motion_turns_on_light(cooldown_s=10),
        actor="alice",
    )

    # Re-primed from the current value (motion off, observed before the event above).
    assert h.store.states[(automation.id, 0)].matched is False
    assert await h.engine.handle_event("2-0", motion(True, 6)) == 1


# --- Engine: timers, schedules, housekeeping --------------------------------------------


async def test_a_hold_fires_from_the_timer_sweep_once_it_has_elapsed() -> None:
    h = harness()
    await prime_motion_off(h)
    triggers = [
        {
            "type": "telemetry",
            "device_id": "motion-1",
            "metric": "motion",
            "op": "eq",
            "value": True,
            "for_s": 60,
        }
    ]
    await save(h, triggers=triggers)

    assert await h.engine.handle_event("1-0", motion(True)) == 0
    h.clock.advance(seconds=59)
    assert await h.engine.fire_due_timers() == 0
    h.clock.advance(seconds=1)
    assert await h.engine.fire_due_timers() == 1
    assert await h.engine.fire_due_timers() == 0
    assert len(h.commands.issued) == 1


async def test_a_hold_is_cancelled_when_the_trigger_stops_matching() -> None:
    h = harness()
    await prime_motion_off(h)
    triggers = [
        {
            "type": "telemetry",
            "device_id": "motion-1",
            "metric": "motion",
            "op": "eq",
            "value": True,
            "for_s": 60,
        }
    ]
    await save(h, triggers=triggers)

    await h.engine.handle_event("1-0", motion(True, 0))
    await h.engine.handle_event("2-0", motion(False, 30))
    h.clock.advance(minutes=5)

    assert await h.engine.fire_due_timers() == 0


async def test_a_schedule_runs_at_its_slot_and_moves_to_the_next_day() -> None:
    h = harness()
    automation = await save(h, triggers=[{"type": "schedule", "at": "09:30"}])  # 12:30 UTC
    slot = h.store.schedules[(automation.id, 0)]
    assert slot == NOW + timedelta(minutes=30)

    h.clock.current = slot + timedelta(seconds=3)
    assert await h.engine.fire_due_schedules() == 1
    assert h.store.schedules[(automation.id, 0)] == slot + timedelta(days=1)
    [run] = h.store.runs.values()
    assert run.event_key == f"schedule:0:{slot.isoformat()}"


async def test_a_slot_missed_by_more_than_the_grace_period_is_skipped() -> None:
    h = harness()
    automation = await save(h, triggers=[{"type": "schedule", "at": "09:30"}])
    slot = h.store.schedules[(automation.id, 0)]

    h.clock.current = slot + timedelta(hours=2)
    assert await h.engine.fire_due_schedules() == 0
    assert h.store.runs == {}
    assert h.store.schedules[(automation.id, 0)] == slot + timedelta(days=1)


async def test_a_run_interrupted_before_finishing_is_settled_as_failed() -> None:
    h = harness()
    await prime_motion_off(h)
    automation = await save(h)

    async with h.engine._uow() as uow:  # a run recorded, then the worker died
        assert await h.engine._start_run(uow, automation, 0, "event:9-0", 0)
        await uow.commit()
    h.clock.advance(minutes=6)
    settled = await h.engine.settle_interrupted()

    assert settled == 1
    [run] = h.store.runs.values()
    assert run.status is RunStatus.FAILED
    assert run.reason == "interrupted before its actions were confirmed"


async def test_a_deleted_scene_fails_its_step_without_stopping_the_others() -> None:
    h = harness()
    await prime_motion_off(h)
    scene = await h.service.create_scene(
        home_id=HOME, name="Evening", states=[SceneState("plug-1", {"on": True})], actor="alice"
    )
    automation = await save(
        h,
        actions=[
            {"type": "scene", "scene_id": str(scene.id)},
            {"type": "command", "device_id": "light-1", "action": "identify"},
        ],
    )
    del h.store.scenes[scene.id]  # e.g. removed by hand in the database

    await h.engine.handle_event("1-0", motion(True))

    [run] = [r for r in h.store.runs.values() if r.automation_id == automation.id]
    assert run.status is RunStatus.FAILED
    assert [o.error for o in run.outcomes] == ["scene deleted", None]
