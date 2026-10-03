"""Electricity tariffs: a base price per kWh and optional time-of-use periods.

Prices are `Decimal` end to end (Postgres `numeric`): a tariff like R$ 0,891234/kWh is
exact, and costs are rounded once, to the currency's minor unit, when they are reported.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from smarthome.modules.energy.domain.errors import InvalidTariff

MAX_PERIODS = 12
MAX_NAME = 40
MAX_PRICE = Decimal(1000)
PRICE_DECIMALS = 6
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# Whole hours only: consumption is accounted per hour, so a period starting at 17:30
# could not be priced honestly.
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):00$|^24:00$")
# ISO 4217 minor units for the currencies that are not 2. Anything else rounds to cents.
_MINOR_UNIT_EXPONENT = {"JPY": 0, "KRW": 0, "CLP": 0, "ISK": 0, "BHD": 3, "KWD": 3, "TND": 3}


def minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def parse_price(raw: Any, *, field: str) -> Decimal:
    if isinstance(raw, float):
        # A float has already lost the exact value the user typed.
        raise InvalidTariff(f'{field}: send prices as strings, e.g. "0.891234"')
    try:
        price = Decimal(str(raw))
    except InvalidOperation as exc:
        raise InvalidTariff(f"{field}: not a number") from exc
    if not price.is_finite() or price < 0 or price > MAX_PRICE:
        raise InvalidTariff(f"{field}: must be between 0 and {MAX_PRICE}")
    exponent = price.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > PRICE_DECIMALS:
        raise InvalidTariff(f"{field}: at most {PRICE_DECIMALS} decimal places")
    return price


@dataclass(frozen=True, slots=True)
class Period:
    """A price that applies on some weekdays (0 = Monday) between two local times.

    `end` is exclusive and may be 24:00; a period never wraps past midnight (split it
    in two instead), which keeps "which price applies at 23:30 on a Sunday" obvious.
    """

    name: str
    weekdays: frozenset[int]
    start: str
    end: str
    price: Decimal

    def __post_init__(self) -> None:
        if not 1 <= len(self.name) <= MAX_NAME:
            raise InvalidTariff(f"period name must have 1 to {MAX_NAME} characters")
        if not self.weekdays or not self.weekdays <= set(range(7)):
            raise InvalidTariff(f"{self.name}: weekdays are 0 (Monday) to 6 (Sunday)")
        for value in (self.start, self.end):
            if not _HHMM.match(value):
                raise InvalidTariff(f"{self.name}: times are whole hours, HH:00 (24:00 as an end)")
        if self.start == "24:00" or minutes(self.start) >= minutes(self.end):
            raise InvalidTariff(f"{self.name}: start must be before end (no wrap past midnight)")

    def covers(self, local: datetime) -> bool:
        minute = local.hour * 60 + local.minute
        return local.weekday() in self.weekdays and minutes(self.start) <= minute < minutes(
            self.end
        )

    def overlaps(self, other: "Period") -> bool:
        return bool(self.weekdays & other.weekdays) and (
            minutes(self.start) < minutes(other.end) and minutes(other.start) < minutes(self.end)
        )


@dataclass(frozen=True, slots=True)
class Tariff:
    home_id: UUID
    currency: str
    base_price: Decimal
    periods: tuple[Period, ...]
    monthly_budget_kwh: Decimal | None
    timezone: str
    version: int
    updated_by: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if not _CURRENCY.match(self.currency):
            raise InvalidTariff("currency must be an ISO 4217 code such as BRL")
        if len(self.periods) > MAX_PERIODS:
            raise InvalidTariff(f"at most {MAX_PERIODS} periods")
        for i, period in enumerate(self.periods):
            for other in self.periods[i + 1 :]:
                if period.overlaps(other):
                    raise InvalidTariff(f"periods {period.name!r} and {other.name!r} overlap")
        if self.monthly_budget_kwh is not None and not (
            Decimal(0) < self.monthly_budget_kwh <= Decimal(1_000_000)
        ):
            raise InvalidTariff("monthly budget must be between 0 and 1000000 kWh")

    def price_at(self, local: datetime) -> Decimal:
        """Price per kWh for an hour starting at `local` (home time)."""
        return next((p.price for p in self.periods if p.covers(local)), self.base_price)


def minor_unit(currency: str) -> Decimal:
    return Decimal(1).scaleb(-_MINOR_UNIT_EXPONENT.get(currency, 2))


def round_money(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(minor_unit(currency), rounding=ROUND_HALF_EVEN)
