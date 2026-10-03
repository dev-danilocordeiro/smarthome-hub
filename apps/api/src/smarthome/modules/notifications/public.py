"""Public interface of the `notifications` module.

The only import path other modules may use. Everything else in this package is private.
Nothing outside the module needs it today: alerts are raised from device events and
energy budgets, which this module reads through the other modules' public interfaces.
"""

__all__: list[str] = []
