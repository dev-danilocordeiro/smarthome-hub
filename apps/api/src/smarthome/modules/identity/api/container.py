from dataclasses import dataclass

from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.infrastructure.sessions import SessionManager
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings


@dataclass(frozen=True, slots=True)
class IdentityModule:
    """Everything the identity HTTP layer needs, built once per process by `wiring.build`."""

    service: IdentityService
    sessions: SessionManager
    settings: Settings
    clock: Clock
