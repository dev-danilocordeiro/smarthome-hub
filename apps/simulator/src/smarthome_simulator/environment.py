"""A small physical and social model of one home, so readings move together believably.

Outdoor temperature follows a daily sine, sunlight follows the sun, residents are home
mostly in the evening and at night, and indoor temperature drifts toward the thermostat
target. Every device reads from the same model, so a motion event, a light turning on
and a jump in power draw line up the way they would in a real house.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DAY_SECONDS = 86_400


@dataclass
class HomeEnvironment:
    rng: random.Random
    started_at: datetime
    speed: float = 1.0  # simulated seconds per real second
    indoor_temp_c: float = 22.0
    base_load_w: float = field(default=0.0)
    # Latest draw of every powered device in the home; the energy meter sums it.
    power_w: dict[str, float] = field(default_factory=dict)
    _away_until: datetime | None = None

    def __post_init__(self) -> None:
        self.base_load_w = self.rng.uniform(60, 140)  # router, standby, etc.

    def sim_time(self, real_now: datetime) -> datetime:
        return self.started_at + (real_now - self.started_at) * self.speed

    @staticmethod
    def hour(t: datetime) -> float:
        return t.hour + t.minute / 60 + t.second / 3600

    def outdoor_temp_c(self, t: datetime) -> float:
        # Coldest around 05:00, warmest around 15:00.
        return 19 + 7 * math.sin(2 * math.pi * (self.hour(t) - 9) / 24)

    def daylight_lux(self, t: datetime) -> float:
        h = self.hour(t)
        if not 6 <= h <= 18:
            return 0.0
        return 30_000 * math.sin(math.pi * (h - 6) / 12) * self.rng.uniform(0.6, 1.0)

    def is_dark(self, t: datetime) -> bool:
        return self.daylight_lux(t) < 300

    def occupied(self, t: datetime) -> bool:
        if self._away_until and t < self._away_until:
            return False
        h = self.hour(t)
        weekend = t.weekday() >= 5
        if weekend:
            return not 10 <= h <= 13  # out for lunch
        return h < 8 or h >= 18

    def maybe_leave(self, t: datetime) -> None:
        """Occasional unplanned outings, which is what makes 'door opened while away' happen."""
        if self._away_until is None and self.rng.random() < 0.002:
            self._away_until = t + timedelta(minutes=self.rng.uniform(20, 180))
        elif self._away_until and t >= self._away_until:
            self._away_until = None

    def step_indoor_temp(
        self, t: datetime, *, target_c: float, hvac_on: bool, dt_s: float
    ) -> float:
        outdoor = self.outdoor_temp_c(t)
        drift_to = target_c if hvac_on else outdoor + 4
        rate = 0.0008 if hvac_on else 0.0002  # first-order approach per simulated second
        self.indoor_temp_c += (drift_to - self.indoor_temp_c) * min(1.0, rate * dt_s)
        return self.indoor_temp_c
