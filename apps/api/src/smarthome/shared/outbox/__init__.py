"""Transactional outbox shared by every module (ADR 0009).

A module appends messages through the `Outbox` port inside its own unit of work, so a
message exists if and only if the change that produced it was committed. The worker
relays them to the broker afterwards, at least once.
"""

from smarthome.shared.outbox.model import Outbox, OutboxMessage

__all__ = ["Outbox", "OutboxMessage"]
