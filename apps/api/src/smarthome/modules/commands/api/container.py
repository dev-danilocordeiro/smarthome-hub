from dataclasses import dataclass

from smarthome.modules.commands.application.services import CommandsService
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings


@dataclass(frozen=True, slots=True)
class CommandsModule:
    service: CommandsService
    settings: Settings
    clock: Clock
