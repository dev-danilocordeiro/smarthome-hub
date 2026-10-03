class EnergyError(Exception):
    pass


class InvalidTariff(EnergyError):
    pass


class InvalidRange(EnergyError):
    pass


class VersionMismatch(EnergyError):
    pass


class VersionRequired(EnergyError):
    """Editing an existing tariff without saying which version the edit started from."""
