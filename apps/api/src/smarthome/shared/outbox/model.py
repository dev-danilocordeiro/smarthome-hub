from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """An MQTT message to publish once the transaction that wrote it commits."""

    topic: str
    payload: dict[str, Any]
    qos: int = 1
    # Past this instant the message is dropped instead of published (e.g. a command
    # the device would have to reject as expired anyway).
    expires_at: datetime | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Outbox(Protocol):
    async def add(self, message: OutboxMessage, *, created_at: datetime) -> None: ...
