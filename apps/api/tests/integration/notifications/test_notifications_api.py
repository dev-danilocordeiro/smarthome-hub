"""The alerts, inbox, preferences and webhook HTTP API."""

from datetime import timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.notifications.application.alerts import AlertEngine
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from tests.integration.notifications.conftest import Household, MovableClock


async def low_battery(
    engine: AlertEngine, household: Household, device: str, clock: MovableClock
) -> None:
    await engine.handle_event(
        "1-0",
        DeviceEvent(
            household.home,
            household.devices[device],
            DeviceEventKind.TELEMETRY,
            clock.now(),
            {"battery_pct": 7.0},
        ),
    )


async def test_members_list_and_acknowledge_alerts_and_a_guest_sees_only_their_devices(
    api: httpx.AsyncClient, household: Household, alert_engine: AlertEngine, clock: MovableClock
) -> None:
    await low_battery(alert_engine, household, "sensor", clock)
    await low_battery(alert_engine, household, "lock", clock)
    url = f"/homes/{household.home}/alerts"

    listed = (await api.get(url, headers=household.viewer.headers)).json()
    assert {a["device_id"] for a in listed} == {
        household.devices["sensor"],
        household.devices["lock"],
    }
    guest_view = (await api.get(url, headers=household.guest.headers)).json()
    assert [a["device_id"] for a in guest_view] == [household.devices["lock"]]
    lock_alert = guest_view[0]
    assert lock_alert["severity"] == "critical"

    acked = await api.post(
        f"{url}/{lock_alert['id']}/acknowledge", headers=household.resident.headers
    )
    assert acked.status_code == 200
    assert acked.json()["acknowledged_by"] == household.resident.principal.user_id
    assert acked.json()["status"] == "open"
    refused = await api.post(
        f"{url}/{lock_alert['id']}/acknowledge", headers=household.guest.headers
    )
    assert refused.status_code == 403
    assert (
        await api.get(url, params={"status": "resolved"}, headers=household.owner.headers)
    ).json() == []


async def test_the_inbox_counts_unread_marks_read_and_hides_homes_the_member_left(
    api: httpx.AsyncClient, household: Household, alert_engine: AlertEngine, clock: MovableClock
) -> None:
    await low_battery(alert_engine, household, "sensor", clock)
    clock.at += timedelta(seconds=1)
    await low_battery(alert_engine, household, "lock", clock)
    headers = household.resident.headers

    inbox = (await api.get("/me/notifications", headers=headers)).json()
    assert inbox["unread"] == 2
    assert [n["title"] for n in inbox["items"]] == [
        "Lock battery is low (7%)",
        "Sensor battery is low (7%)",
    ]
    page = (
        await api.get(
            "/me/notifications", params={"before": inbox["items"][0]["created_at"]}, headers=headers
        )
    ).json()
    assert [n["title"] for n in page["items"]] == ["Sensor battery is low (7%)"]

    first = inbox["items"][0]["id"]
    assert (await api.post(f"/me/notifications/{first}/read", headers=headers)).status_code == 204
    assert (await api.get("/me/notifications/unread-count", headers=headers)).json() == {
        "unread": 1
    }
    # Someone else's notification is not found, not "forbidden".
    other = await api.post(f"/me/notifications/{first}/read", headers=household.owner.headers)
    assert other.status_code == 404
    assert (await api.post("/me/notifications/read-all", headers=headers)).json() == {"marked": 1}

    await household.identity.revoke_member(
        household.owner.principal, household.home, household.resident.principal.user_id
    )
    after = (await api.get("/me/notifications", headers=headers)).json()
    assert after == {"items": [], "unread": 0}


async def test_preferences_default_then_save_and_reject_half_quiet_hours(
    api: httpx.AsyncClient, household: Household
) -> None:
    url = f"/homes/{household.home}/notification-preferences"
    headers = household.owner.headers
    assert (await api.get(url, headers=headers)).json() == {
        "email_enabled": True,
        "min_email_severity": "warning",
        "quiet_start": None,
        "quiet_end": None,
    }
    body = {
        "email_enabled": True,
        "min_email_severity": "critical",
        "quiet_start": 22,
        "quiet_end": 7,
    }
    assert (await api.put(url, json=body, headers=headers)).status_code == 200
    assert (await api.get(url, headers=headers)).json() == body
    half = await api.put(url, json=body | {"quiet_end": None}, headers=headers)
    assert half.status_code == 422
    assert (await api.get(url, headers=household.guest.headers)).status_code == 403


async def test_quiet_hours_and_severity_shape_what_is_emailed(
    engine: AsyncEngine,
    api: httpx.AsyncClient,
    household: Household,
    alert_engine: AlertEngine,
    clock: MovableClock,
) -> None:
    await api.put(
        f"/homes/{household.home}/notification-preferences",
        json={"email_enabled": True, "min_email_severity": "critical"},
        headers=household.owner.headers,
    )
    await low_battery(alert_engine, household, "sensor", clock)  # warning
    await low_battery(alert_engine, household, "lock", clock)  # critical

    async with engine.connect() as conn:
        targets = (
            await conn.execute(
                text(
                    "SELECT d.target, a.severity FROM notifications.deliveries d"
                    " JOIN notifications.alerts a ON a.id = d.alert_id WHERE d.home_id = :home"
                ),
                {"home": household.home},
            )
        ).all()
    owner = f"{household.owner.principal.user_id}@example.com"
    assert sorted(severity for target, severity in targets if target == owner) == ["critical"]


async def test_the_webhook_secret_is_shown_once_and_only_owners_manage_it(
    api: httpx.AsyncClient, household: Household
) -> None:
    url = f"/homes/{household.home}/webhook"
    headers = household.owner.headers
    hook = "https://hooks.example.com/smarthome"

    created = (await api.put(url, json={"url": hook}, headers=headers)).json()
    assert len(created["secret"]) >= 40
    edited = (await api.put(url, json={"url": hook, "enabled": False}, headers=headers)).json()
    assert edited["secret"] is None
    assert edited["enabled"] is False
    fetched = (await api.get(url, headers=headers)).json()
    assert "secret" not in fetched or fetched["secret"] is None
    rotated = (
        await api.put(url, json={"url": hook, "rotate_secret": True}, headers=headers)
    ).json()
    assert rotated["secret"] not in (None, created["secret"])

    assert (
        await api.put(url, json={"url": hook}, headers=household.resident.headers)
    ).status_code == 403
    assert (await api.put(url, json={"url": "ftp://x"}, headers=headers)).status_code == 422
    assert (await api.delete(url, headers=headers)).status_code == 204
    assert (await api.get(url, headers=headers)).status_code == 404
    assert (await api.post(f"{url}/test", headers=headers)).status_code == 404
