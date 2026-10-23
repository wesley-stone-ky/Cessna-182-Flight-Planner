"""Sunrise / sunset for a place, on the local calendar day of a given instant."""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

DEFAULT_TZ = "America/New_York"


@lru_cache(maxsize=4096)
def _sun(lat: float, lon: float, day: date, tz: str) -> tuple[datetime, datetime]:
    s = sun(Observer(latitude=lat, longitude=lon), date=day, tzinfo=ZoneInfo(tz))
    return s["sunrise"], s["sunset"]


def sun_times(lat: float, lon: float, when: datetime, tz: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """(sunrise, sunset) as aware datetimes for the local day containing `when`."""
    local_day = when.astimezone(ZoneInfo(tz)).date()
    return _sun(round(lat, 2), round(lon, 2), local_day, tz)
