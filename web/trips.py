"""Trip mode for the web app: best days, times and routes between two airports.

Same engine as the Streamlit trip planner (planner.candidate_plans -> attach_terrain
-> search_days), with each search cached for 15 minutes so refreshing the page
is free.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from flightplanner.aircraft import Aircraft
from flightplanner.airports import Airport, AirportDB
from flightplanner.config import Minimums
from flightplanner.export import leg_fpl, leg_gfp
from flightplanner.planner import Context, DayResult, Leg, attach_terrain, candidate_plans, forecast_points, search_days
from flightplanner.terrain import Terrain
from flightplanner.weather import AviationWeather

MAX_ROUTES = 4
CACHE_S = 900


@dataclass
class TripResult:
    dep: Airport
    dest: Airport
    days: list[DayResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""
    tz: str = "America/New_York"


def official_products(airports: list[Airport], db: AirportDB) -> tuple[dict, list, list[str]]:
    """TAFs (borrowing the nearest when an airport has none) and AIRMET/SIGMETs; notes for anything missing."""
    awc = AviationWeather(ttl_s=240)
    _, tafs = awc.for_airports(airports, db.nearby)
    advs = awc.advisories()
    notes = [f"Couldn't reach aviationweather.gov for {k}." for k, v in awc.status.items() if not v.ok]
    return tafs, advs, notes


class TripPlanner:
    def __init__(self, db: AirportDB, forecast, terrain: Terrain | None = None, aviation=official_products):
        self.db, self.forecast, self.aviation = db, forecast, aviation
        self.terrain = terrain or Terrain()
        self._cache: dict[str, tuple[float, TripResult]] = {}

    def plan(self, dep: Airport, dest: Airport, ac: Aircraft, mins: Minimums, start: date, end: date,
             earliest: int, latest: int) -> TripResult:
        key = hashlib.sha1(json.dumps([dep.ident, dest.ident, ac.raw, ac.cruise_setting, mins.__dict__,
                                       str(start), str(end), earliest, latest], sort_keys=True,
                                      default=str).encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        res = self._plan(dep, dest, ac, mins, start, end, earliest, latest)
        if not res.error:
            self._cache[key] = (time.time(), res)
        return res

    def _plan(self, dep, dest, ac, mins, start, end, earliest, latest) -> TripResult:
        res = TripResult(dep, dest, tz=dep.tz)
        plans = candidate_plans(self.db, dep, dest, ac, mins)[:MAX_ROUTES]
        if not plans:
            res.error = ("No route works within your fuel and leg-time limits. Check your usable fuel, or allow "
                         "a longer leg in Settings.")
            return res
        self.terrain.used_fallback = False
        attach_terrain(plans, self.terrain, mins)
        if self.terrain.used_fallback:
            res.notes.append("Couldn't reach the terrain data; using conservative built-in terrain heights.")
        pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
        try:
            fc = self.forecast(pts)
        except Exception:
            res.error = "We couldn't reach the National Weather Service just now. Try again in a few minutes."
            return res
        aps = list({a.ident: a for p in plans for a in p.airports}.values())
        try:
            tafs, advs, notes = self.aviation(aps, self.db)
            res.notes += notes
        except Exception:
            tafs, advs = {}, []
            res.notes.append("Couldn't reach aviationweather.gov; TAFs and AIRMET/SIGMETs aren't included.")
        ctx = Context(ac, mins, fc, self.db, tafs=tafs, advisories=advs, tz=dep.tz, now=datetime.now(timezone.utc))
        res.days = search_days(ctx, plans, start, end, earliest, latest)
        return res


def route_zip(db: AirportDB, idents: list[str]) -> tuple[str, bytes]:
    """(filename, zip bytes) with a Garmin .fpl and GTN .gfp for each leg of the route."""
    aps = [db.get(i) for i in idents]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for a, b in zip(aps, aps[1:]):
            leg = Leg(a, b)
            z.writestr(f"{a.ident}-{b.ident}.fpl", leg_fpl(leg))
            z.writestr(f"{a.ident}-{b.ident}.gfp", leg_gfp(leg))
    return f"flightplan_{'-'.join(a.ident for a in aps)}.zip", buf.getvalue()
