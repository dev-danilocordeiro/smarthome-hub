"""Public interface of the `identity` module.

Other modules authenticate and authorize through these names only:

    CanControl = Depends(require_home_access(Permission.CONTROL_DEVICES))

    @router.post("/homes/{home_id}/devices/{device_id}/commands")
    async def send(access: Annotated[HomeAccess, CanControl]): ...
"""

from smarthome.modules.identity.api.dependencies import (
    CurrentPrincipal,
    require_home_access,
)
from smarthome.modules.identity.application.services import HomeAccess
from smarthome.modules.identity.domain.model import HomeId, Permission, Role, UserId
from smarthome.modules.identity.domain.principal import Principal

__all__ = [
    "CurrentPrincipal",
    "HomeAccess",
    "HomeId",
    "Permission",
    "Principal",
    "Role",
    "UserId",
    "require_home_access",
]
