"""Public interface of the `energy` module.

The only import path other modules may use. Everything else in this package is private.
Notifications read month-to-date budgets through `EnergyService.budgets`.
"""

from smarthome.modules.energy.application.services import EnergyService
from smarthome.modules.energy.domain.usage import BudgetStatus

__all__ = ["BudgetStatus", "EnergyService"]
