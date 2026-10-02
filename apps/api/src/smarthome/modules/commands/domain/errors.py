class CommandsError(Exception):
    pass


class CommandNotFound(CommandsError):
    """Also raised for commands of another home or device, so ids cannot be probed."""


class TargetNotFound(CommandsError):
    """The device does not exist in this home (or was unpaired)."""


class TargetUnavailable(CommandsError):
    """The device exists but does not accept commands (quarantined)."""


class InvalidCommand(CommandsError):
    pass
