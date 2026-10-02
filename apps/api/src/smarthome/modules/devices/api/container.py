from dataclasses import dataclass

from smarthome.modules.devices.application.services import DevicesService
from smarthome.shared.config import Settings
from smarthome.shared.http.rate_limit import RateLimiter


@dataclass(frozen=True, slots=True)
class DevicesModule:
    service: DevicesService
    rate_limiter: RateLimiter
    settings: Settings
