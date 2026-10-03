from dataclasses import dataclass

from smarthome.modules.automations.application.services import AutomationsService
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings


@dataclass(frozen=True, slots=True)
class AutomationsModule:
    service: AutomationsService
    settings: Settings
    clock: Clock
