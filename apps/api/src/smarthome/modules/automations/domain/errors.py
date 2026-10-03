class AutomationsError(Exception):
    pass


class InvalidDefinition(AutomationsError):
    """The automation or scene does not follow the DSL, or targets what it cannot."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


class AutomationNotFound(AutomationsError):
    """Also raised for automations of another home, so ids cannot be probed."""


class SceneNotFound(AutomationsError):
    pass


class SceneInUse(AutomationsError):
    """A scene referenced by automations cannot be deleted."""


class VersionMismatch(AutomationsError):
    """The client edited a version that is no longer current (optimistic concurrency)."""


class InvalidRange(AutomationsError):
    pass


class NameTaken(AutomationsError):
    """Names are unique per home (automations and scenes separately)."""
