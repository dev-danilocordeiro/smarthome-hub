import pytest
from hypothesis import given
from hypothesis import strategies as st

from device_protocol import (
    DELIVERY,
    DeviceTopic,
    InvalidTopic,
    MessageKind,
    device_publishes,
    device_subscribes,
    hub_subscription,
    parse,
    topic,
)

ids = st.from_regex(r"[A-Za-z0-9_-]{1,64}", fullmatch=True)


@given(home=ids, device=ids, kind=st.sampled_from(MessageKind))
def test_every_topic_parses_back_to_what_built_it(
    home: str, device: str, kind: MessageKind
) -> None:
    assert parse(topic(home, device, kind)) == DeviceTopic(home, device, kind)


@pytest.mark.parametrize(
    "value",
    [
        "v2/homes/h/devices/d/telemetry",
        "v1/homes/h/devices/d/unknown",
        "v1/homes/h/devices/d",
        "v1/houses/h/devices/d/state",
        "v1/homes/+/devices/d/state",
        "v1/homes/h/devices/#/state",
        "v1/homes/h/devices/d/commands/ack/extra",
        "v1/homes//devices/d/state",
    ],
)
def test_malformed_or_wildcard_topics_are_rejected(value: str) -> None:
    with pytest.raises(InvalidTopic):
        parse(value)


@pytest.mark.parametrize("bad", ["a/b", "+", "#", "", "x" * 65, "café"])
def test_ids_that_could_escape_their_topic_segment_are_refused(bad: str) -> None:
    with pytest.raises(InvalidTopic):
        topic(bad, "device", MessageKind.STATE)


def test_every_message_kind_has_a_declared_delivery() -> None:
    assert set(DELIVERY) == set(MessageKind)


def test_telemetry_is_fire_and_forget_while_everything_else_is_at_least_once() -> None:
    assert DELIVERY[MessageKind.TELEMETRY].qos == 0
    assert all(d.qos == 1 for k, d in DELIVERY.items() if k is not MessageKind.TELEMETRY)


def test_only_last_known_values_are_retained() -> None:
    retained = {k for k, d in DELIVERY.items() if d.retain}
    assert retained == {MessageKind.STATE, MessageKind.PRESENCE}


def test_a_device_acl_covers_exactly_its_own_topics() -> None:
    pubs = device_publishes("h1", "d1")
    subs = device_subscribes("h1", "d1")

    assert sorted(pubs) == sorted(
        f"v1/homes/h1/devices/d1/{k}" for k in ("telemetry", "state", "commands/ack", "presence")
    )
    assert subs == ["v1/homes/h1/devices/d1/commands"]
    assert all("+" not in t and "#" not in t for t in pubs + subs)


def test_hub_subscriptions_can_be_shared_across_ingestor_replicas() -> None:
    assert hub_subscription(MessageKind.TELEMETRY) == "v1/homes/+/devices/+/telemetry"
    assert (
        hub_subscription(MessageKind.TELEMETRY, share_group="ingestor")
        == "$share/ingestor/v1/homes/+/devices/+/telemetry"
    )
