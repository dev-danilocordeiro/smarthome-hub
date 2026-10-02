import hashlib
import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from smarthome.modules.devices.public import DeviceNotFound, DevicesService
from smarthome.modules.identity.public import HomeAccess, Permission, require_home_access
from smarthome.modules.telemetry.application.services import TelemetryQueries
from smarthome.modules.telemetry.domain.model import Resolution
from smarthome.shared.config import Settings

router = APIRouter(tags=["telemetry"])
CanView = Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))]


class TelemetryModule(BaseModel, arbitrary_types_allowed=True):
    queries: TelemetryQueries
    devices: DevicesService
    redis: Any
    settings: Settings


def module(request: Request) -> TelemetryModule:
    found: TelemetryModule = request.app.state.telemetry
    return found


Telemetry = Annotated[TelemetryModule, Depends(module)]


class PointOut(BaseModel):
    time: datetime
    avg: float
    min: float
    max: float
    samples: int


class SeriesOut(BaseModel):
    device_id: str
    metric: str
    resolution: Resolution
    points: list[PointOut]


class LatestOut(BaseModel):
    device_id: str
    readings: dict[str, dict[str, Any]] = Field(description="metric -> {value, at}")


async def _visible_device(access: HomeAccess, telemetry: TelemetryModule, device_id: str) -> None:
    """404 unless the device is in this home and, for scoped guests, in their scope."""
    scope = access.membership.device_scope
    if scope is not None and device_id not in scope:
        raise DeviceNotFound(device_id)
    await telemetry.devices.get(access.home.id, device_id)


@router.get("/homes/{home_id}/devices/{device_id}/telemetry")
async def history(
    *,
    device_id: str,
    access: CanView,
    telemetry: Telemetry,
    metric: Annotated[str, Query(pattern=r"^[a-z_]{1,40}$")],
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime | None, Query(alias="to")] = None,
    resolution: Resolution | None = None,
) -> SeriesOut:
    """History of one metric. Without `resolution`, the range picks it: raw up to 2 h,
    1-minute buckets up to a day, hourly up to 60 days, daily beyond."""
    await _visible_device(access, telemetry, device_id)
    cache_ttl = telemetry.settings.telemetry_history_cache_s
    key = (
        "telemetry:series:"
        + hashlib.sha256(
            json.dumps(
                [str(access.home.id), device_id, metric, str(start), str(end), resolution]
            ).encode()
        ).hexdigest()
    )
    if end is not None and cache_ttl and (cached := await telemetry.redis.get(key)):
        return SeriesOut.model_validate_json(cached)
    chosen, points = await telemetry.queries.series(
        home_id=access.home.id,
        device_id=device_id,
        metric=metric,
        start=start,
        end=end,
        resolution=resolution,
    )
    out = SeriesOut(
        device_id=device_id,
        metric=metric,
        resolution=chosen,
        points=[
            PointOut(time=p.time, avg=p.avg, min=p.min, max=p.max, samples=p.samples)
            for p in points
        ],
    )
    if end is not None and cache_ttl:
        # Only closed ranges are cacheable; "until now" changes every second.
        await telemetry.redis.set(key, out.model_dump_json(), ex=cache_ttl)
    return out


@router.get("/homes/{home_id}/devices/{device_id}/readings/latest")
async def latest(device_id: str, access: CanView, telemetry: Telemetry) -> LatestOut:
    """Last value of every metric, from Redis (never the time-series store)."""
    await _visible_device(access, telemetry, device_id)
    return LatestOut(device_id=device_id, readings=await telemetry.queries.latest(device_id))
