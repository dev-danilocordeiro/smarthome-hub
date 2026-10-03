from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from smarthome.modules.energy.application.services import EnergyService
from smarthome.modules.energy.domain.tariff import Period, Tariff, parse_price
from smarthome.modules.energy.domain.usage import Bucket, MeasuredBy
from smarthome.modules.identity.public import HomeAccess, Permission, require_home_access
from smarthome.shared.http.preconditions import etag, expected_version

router = APIRouter(tags=["energy"])


class NoTariff(Exception):
    pass


class GuestsExcluded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class EnergyModule:
    service: EnergyService


def energy_module(request: Request) -> EnergyModule:
    module: EnergyModule = request.app.state.energy
    return module


Energy = Annotated[EnergyModule, Depends(energy_module)]
CanView = Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))]
CanManage = Annotated[HomeAccess, Depends(require_home_access(Permission.MANAGE_DEVICES))]

Price = Annotated[
    str,
    Field(
        pattern=r"^\d{1,4}(\.\d{1,6})?$",
        description="Price per kWh as a decimal string (exact; never a float).",
        examples=["0.891234"],
    ),
]


def household_only(access: HomeAccess) -> None:
    """A guest pass is for some devices; the whole home's consumption is not part of it."""
    if access.membership.device_scope is not None:
        raise GuestsExcluded


class BucketOut(BaseModel):
    start: datetime
    wh: float
    cost: str | None


class DeviceUsageOut(BaseModel):
    device_id: str
    name: str | None
    room: str | None
    whole_home: bool
    wh: float
    cost: str | None


class UsageOut(BaseModel):
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    bucket: Bucket
    timezone: str
    currency: str | None
    measured_by: MeasuredBy = Field(
        description="`meter` when a whole-home meter reported in the range (the total is the"
        " meter), else `submeters` (the total is the sum of the plugs)."
    )
    total_wh: float
    total_cost: str | None
    unmetered_wh: float | None = Field(description="Meter minus plugs; null without a meter.")
    buckets: list[BucketOut]
    devices: list[DeviceUsageOut]


class PeriodIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    weekdays: list[int] = Field(min_length=1, max_length=7, description="0 = Monday")
    start: str = Field(examples=["18:00"])
    end: str = Field(examples=["21:00"])
    price: Price


class TariffIn(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$", examples=["BRL"])
    base_price: Price
    periods: list[PeriodIn] = Field(default_factory=list, max_length=12)
    monthly_budget_kwh: Annotated[str, Field(pattern=r"^\d{1,7}(\.\d{1,3})?$")] | None = None


class PeriodOut(BaseModel):
    name: str
    weekdays: list[int]
    start: str
    end: str
    price: str


class TariffOut(BaseModel):
    currency: str
    base_price: str
    periods: list[PeriodOut]
    monthly_budget_kwh: str | None
    timezone: str
    version: int
    updated_by: str
    updated_at: datetime


def _tariff_out(tariff: Tariff) -> TariffOut:
    return TariffOut(
        currency=tariff.currency,
        base_price=str(tariff.base_price),
        periods=[
            PeriodOut(
                name=p.name,
                weekdays=sorted(p.weekdays),
                start=p.start,
                end=p.end,
                price=str(p.price),
            )
            for p in tariff.periods
        ],
        monthly_budget_kwh=(
            str(tariff.monthly_budget_kwh) if tariff.monthly_budget_kwh is not None else None
        ),
        timezone=tariff.timezone,
        version=tariff.version,
        updated_by=tariff.updated_by,
        updated_at=tariff.updated_at,
    )


@router.get("/homes/{home_id}/energy/usage", response_model_by_alias=True)
async def usage(
    *,
    access: CanView,
    energy: Energy,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    bucket: Bucket = Bucket.DAY,
) -> UsageOut:
    """Consumption (and cost, once a tariff is set) in hourly or daily buckets of home
    time, with a per-device breakdown. Up to 400 days."""
    household_only(access)
    timezone = access.home.timezone
    report, devices = await energy.service.usage(
        access.home.id, timezone=timezone, start=start, end=end, bucket=bucket
    )
    known = {d.id: d for d in devices}

    def money(amount: Decimal | None) -> str | None:
        return str(amount) if amount is not None else None

    return UsageOut(
        start=report.start,
        end=report.end,
        bucket=report.bucket,
        timezone=timezone,
        currency=report.currency,
        measured_by=report.measured_by,
        total_wh=report.total_wh,
        total_cost=money(report.total_cost),
        unmetered_wh=report.unmetered_wh,
        buckets=[BucketOut(start=b.start, wh=b.wh, cost=money(b.cost)) for b in report.buckets],
        devices=[
            DeviceUsageOut(
                device_id=d.device_id,
                name=known[d.device_id].name if d.device_id in known else None,
                room=known[d.device_id].room if d.device_id in known else None,
                whole_home=d.whole_home,
                wh=d.wh,
                cost=money(d.cost),
            )
            for d in report.devices
        ],
    )


@router.get("/homes/{home_id}/energy/tariff", responses={404: {"description": "No tariff set yet"}})
async def get_tariff(access: CanView, energy: Energy, response: Response) -> TariffOut:
    household_only(access)
    tariff = await energy.service.tariff(access.home.id)
    if tariff is None:
        raise NoTariff
    response.headers["ETag"] = etag(tariff.version)
    return _tariff_out(tariff)


@router.put(
    "/homes/{home_id}/energy/tariff",
    responses={
        412: {"description": "If-Match does not name the current version"},
        428: {"description": "A tariff exists and If-Match is missing"},
    },
)
async def put_tariff(
    *,
    body: TariffIn,
    access: CanManage,
    energy: Energy,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> TariffOut:
    """Create the home's tariff, or replace it (If-Match with the current ETag)."""
    tariff = await energy.service.set_tariff(
        access.home.id,
        timezone=access.home.timezone,
        currency=body.currency,
        base_price=parse_price(body.base_price, field="base_price"),
        periods=tuple(
            Period(
                name=p.name,
                weekdays=frozenset(p.weekdays),
                start=p.start,
                end=p.end,
                price=parse_price(p.price, field=f"periods/{i}/price"),
            )
            for i, p in enumerate(body.periods)
        ),
        monthly_budget_kwh=(
            Decimal(body.monthly_budget_kwh) if body.monthly_budget_kwh is not None else None
        ),
        expected_version=expected_version(if_match, required=False),
        actor=access.membership.user_id,
    )
    response.headers["ETag"] = etag(tariff.version)
    return _tariff_out(tariff)
