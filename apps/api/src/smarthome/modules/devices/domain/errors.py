class DevicesError(Exception):
    pass


class DeviceNotFound(DevicesError):
    """Also raised for devices of another home, so ids cannot be probed across tenants."""


class InvalidPairingCode(DevicesError):
    """Unknown, expired or already used. Deliberately one error for all three."""


class InvalidTransition(DevicesError):
    pass


class UnsupportedState(DevicesError):
    pass
