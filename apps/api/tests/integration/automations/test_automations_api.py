"""The automations and scenes HTTP API: versioned edits, validation errors an editor can
point at, who may automate what, scenes, and the dry run."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.identity.domain.model import Role
from smarthome.modules.telemetry.domain.model import Reading
from smarthome.modules.telemetry.infrastructure.timescale import TimescaleReadings
from tests.integration.automations.conftest import Household
from tests.integration.helpers import Member, sign_in


def motion_lights(household: Household, **overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "triggers": [
            {
                "type": "telemetry",
                "device_id": household.devices["motion"],
                "metric": "motion",
                "op": "eq",
                "value": True,
            }
        ],
        "actions": [
            {
                "type": "command",
                "device_id": household.devices["hall"],
                "action": "set_state",
                "desired": {"on": True},
            }
        ],
    } | overrides


async def member(redis: Redis, household: Household, role: Role, **invite: Any) -> Member:
    joined = await sign_in(redis, f"{role.value}-{secrets.token_hex(4)}")
    _, token = await household.identity.invite(
        household.owner.principal, household.home, role=role, **invite
    )
    await household.identity.accept_invitation(joined.principal, token)
    return joined


async def test_an_automation_is_created_edited_with_if_match_and_deleted(
    api: httpx.AsyncClient, household: Household
) -> None:
    base = f"/homes/{household.home}/automations"
    headers = household.owner.headers
    body = {"name": "Hall light", "definition": motion_lights(household)}

    created = await api.post(base, json=body, headers=headers)
    assert created.status_code == 201, created.text
    automation = created.json()
    url = created.headers["location"]
    assert created.headers["etag"] == '"1"'
    assert automation["timezone"] == "America/Sao_Paulo"
    assert automation["status"] == "active"

    edited = body | {"definition": motion_lights(household, cooldown_s=120)}
    missing = await api.put(url, json=edited, headers=headers)
    stale = await api.put(url, json=edited, headers=headers | {"if-match": '"7"'})
    ok = await api.put(url, json=edited, headers=headers | {"if-match": '"1"'})
    assert missing.status_code == 428
    assert stale.status_code == 412
    assert ok.status_code == 200, ok.text
    assert ok.headers["etag"] == '"2"'
    assert ok.json()["definition"]["cooldown_s"] == 120

    revisions = (await api.get(f"{url}/revisions", headers=headers)).json()
    assert [(r["version"], r["change"]) for r in revisions] == [(2, "updated"), (1, "created")]
    listed = (await api.get(base, headers=headers)).json()
    assert [a["name"] for a in listed] == ["Hall light"]

    disabled = await api.post(f"{url}/disable", headers=headers)
    assert disabled.json()["enabled"] is False
    assert (await api.delete(url, headers=headers | {"if-match": '"2"'})).status_code == 204
    assert (await api.get(url, headers=headers)).status_code == 404
    revisions = (await api.get(f"{url}/revisions", headers=headers)).json()
    assert revisions[0]["change"] == "deleted"


async def test_an_invalid_definition_says_where_the_problem_is(
    api: httpx.AsyncClient, household: Household
) -> None:
    triggers = [
        {
            "type": "telemetry",
            "device_id": household.devices["motion"],
            "metric": "motion",
            "op": "gt",
            "value": 3,
        }
    ]

    response = await api.post(
        f"/homes/{household.home}/automations",
        json={"name": "Broken", "definition": motion_lights(household, triggers=triggers)},
        headers=household.owner.headers,
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["type"] == "urn:smarthome:problem:invalid-automation"
    assert problem["path"].startswith("triggers/0")


async def test_names_are_unique_per_home(api: httpx.AsyncClient, household: Household) -> None:
    url = f"/homes/{household.home}/automations"
    body = {"name": "Night", "definition": motion_lights(household)}

    first = await api.post(url, json=body, headers=household.owner.headers)
    second = await api.post(url, json=body | {"name": "NIGHT"}, headers=household.owner.headers)

    assert first.status_code == 201
    assert second.status_code == 409


async def test_viewers_cannot_automate_and_guests_cannot_see_automations(
    api: httpx.AsyncClient, household: Household, redis: Redis
) -> None:
    url = f"/homes/{household.home}/automations"
    viewer = await member(redis, household, Role.VIEWER)
    guest = await member(
        redis,
        household,
        Role.GUEST,
        guest_access_expires_at=datetime.now(UTC) + timedelta(days=1),
        device_scope=frozenset({household.devices["hall"]}),
    )

    assert (await api.get(url, headers=viewer.headers)).status_code == 200
    create = await api.post(
        url, json={"name": "x", "definition": motion_lights(household)}, headers=viewer.headers
    )
    assert create.status_code == 403
    assert (await api.get(url, headers=guest.headers)).status_code == 403


async def test_automating_a_lock_needs_a_recent_sign_in(
    api: httpx.AsyncClient, household: Household, redis: Redis
) -> None:
    door = household.devices["door"]
    body = {
        "name": "Lock at night",
        "definition": {
            "schema_version": "1",
            "triggers": [{"type": "schedule", "at": "23:00"}],
            "actions": [
                {
                    "type": "command",
                    "device_id": door,
                    "action": "set_state",
                    "desired": {"locked": True},
                }
            ],
        },
    }
    stale = await sign_in(
        redis, household.owner.principal.user_id, authenticated_ago=timedelta(minutes=30)
    )
    url = f"/homes/{household.home}/automations"

    refused = await api.post(url, json=body, headers=stale.headers)
    accepted = await api.post(url, json=body, headers=household.owner.headers)

    assert refused.status_code == 401
    assert refused.json()["type"] == "urn:smarthome:problem:reauthentication-required"
    assert accepted.status_code == 201


async def test_scenes_are_activated_as_the_caller_and_protected_while_in_use(
    api: httpx.AsyncClient, household: Household, redis: Redis
) -> None:
    hall, porch = household.devices["hall"], household.devices["porch"]
    headers = household.owner.headers
    created = await api.post(
        f"/homes/{household.home}/scenes",
        json={
            "name": "Movie",
            "states": [
                {"device_id": hall, "desired": {"on": True, "brightness_pct": 10}},
                {"device_id": porch, "desired": {"on": False}},
            ],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    scene = created.json()

    activated = await api.post(
        f"/homes/{household.home}/scenes/{scene['id']}/activate", headers=headers
    )
    assert activated.status_code == 202, activated.text
    outcomes = activated.json()["outcomes"]
    assert [o["device_id"] for o in outcomes] == [hall, porch]
    assert all(o["command_id"] for o in outcomes)
    command = await api.get(
        f"/homes/{household.home}/devices/{hall}/commands/{outcomes[0]['command_id']}",
        headers=headers,
    )
    assert command.json()["issued_by"] == household.owner.principal.user_id

    guest = await member(
        redis,
        household,
        Role.GUEST,
        guest_access_expires_at=datetime.now(UTC) + timedelta(days=1),
        device_scope=frozenset({hall}),
    )
    assert (await api.get(f"/homes/{household.home}/scenes", headers=guest.headers)).json() == []
    refused = await api.post(
        f"/homes/{household.home}/scenes/{scene['id']}/activate", headers=guest.headers
    )
    assert refused.status_code == 403

    automation = await api.post(
        f"/homes/{household.home}/automations",
        json={
            "name": "Movie at 9",
            "definition": motion_lights(
                household, actions=[{"type": "scene", "scene_id": scene["id"]}]
            ),
        },
        headers=headers,
    )
    assert automation.status_code == 201
    in_use = await api.delete(f"/homes/{household.home}/scenes/{scene['id']}", headers=headers)
    assert in_use.status_code == 409
    assert "Movie at 9" in in_use.json()["detail"]


async def test_a_dry_run_replays_recorded_telemetry(
    api: httpx.AsyncClient, household: Household, engine: AsyncEngine
) -> None:
    climate = household.devices["climate"]
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    temperatures = [24, 25, 27, 28, 25, 24, 29]
    await TimescaleReadings(engine).write(
        [
            Reading(
                start + timedelta(minutes=10 * i),
                household.home,
                climate,
                "temperature_c",
                float(t),
                f"msg-{i}-{secrets.token_hex(4)}",
                start + timedelta(minutes=10 * i),
            )
            for i, t in enumerate(temperatures)
        ]
    )
    definition = motion_lights(
        household,
        triggers=[
            {
                "type": "telemetry",
                "device_id": climate,
                "metric": "temperature_c",
                "op": "gt",
                "value": 26,
            }
        ],
    )

    response = await api.post(
        f"/homes/{household.home}/automations/dry-run",
        json={"definition": definition, "from": start.isoformat()},
        headers=household.owner.headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert [r["outcome"] for r in result["runs"]] == ["would_run", "would_run"]
    assert [datetime.fromisoformat(r["at"]) for r in result["runs"]] == [
        start + timedelta(minutes=20),
        start + timedelta(minutes=60),
    ]
    too_long = await api.post(
        f"/homes/{household.home}/automations/dry-run",
        json={
            "definition": definition,
            "from": (start - timedelta(days=8)).isoformat(),
            "to": start.isoformat(),
        },
        headers=household.owner.headers,
    )
    assert too_long.status_code == 422


async def test_the_dsl_schema_is_published(api: httpx.AsyncClient) -> None:
    response = await api.get("/automations/schema")

    assert response.status_code == 200
    assert response.json()["title"] == "Automation definition"
