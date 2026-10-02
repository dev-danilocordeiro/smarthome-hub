from dataclasses import dataclass
from datetime import datetime, timedelta

from smarthome.modules.identity.domain.model import UserId


@dataclass(frozen=True, slots=True)
class Principal:
    """The signed-in person behind a request, as established by the BFF session."""

    user_id: UserId
    email: str | None
    display_name: str | None
    # When the user last actually authenticated at the IdP (OIDC `auth_time`), not when
    # the session was last refreshed. Critical actions require this to be recent.
    authenticated_at: datetime

    def authenticated_within(self, window: timedelta, *, now: datetime) -> bool:
        return now - self.authenticated_at <= window
