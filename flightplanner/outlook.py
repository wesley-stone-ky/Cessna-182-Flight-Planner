"""10-day outlook: daily flyability per airport and along a route."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .airports import Airport
from .config import Minimums
from .sun import sun_times
from .weather import GridForecast, WxPoint, flight_category


@dataclass
class DayOutlook:
    day: date
    verdict: str              # Good / Fair / Poor / No data
    flyable_hours: int
    daylight_hours: int
    best_window: str          # e.g. "9 AM–3 PM"
    low_ceiling_ft: float | None
    min_vis_sm: float | None
    max_wind_kt: float
    max_gust_kt: float
    rain_pct: float
    storms: str               # low / some / likely
    hi_c: float | None
    lo_c: float | None
    wind_6000: str            # "250@22"
    freezing_level_ft: float | None
    summary: str


def _hour_ok(wx: WxPoint, mins: Minimums) -> bool:
    cig = wx.ceiling_agl_ft()
    if cig is not None and cig < mins.airport_min_ceiling_ft_agl:
        return False
    if wx.vis_sm is not None and wx.vis_sm < mins.airport_min_vis_sm:
        return False
    if wx.gust_kt >= mins.max_gust_kt or wx.wind_kt >= mins.max_surface_wind_kt:
        return False
    if wx.thunder or (wx.cape >= mins.cape_nogo_jkg and wx.precip_prob >= 30):
        return False
    return True


def _fmt_hr(dt: datetime) -> str:
    return dt.strftime("%-I %p")


def _windows(flags: list[tuple[datetime, bool]]) -> str:
    best, cur = (None, None, 0), None
    for t, ok in flags:
        if ok:
            cur = (cur[0], t, cur[2] + 1) if cur else (t, t, 1)
            if cur[2] > best[2]:
                best = cur
        else:
            cur = None
    if not best[0]:
        return "—"
    return f"{_fmt_hr(best[0])}–{_fmt_hr(best[1] + timedelta(hours=1))}"


def airport_outlook(fc: GridForecast, ap: Airport, mins: Minimums, tz: str = "America/New_York",
                    days: int = 10, start: date | None = None) -> list[DayOutlook]:
    zone = ZoneInfo(tz)
    start = start or datetime.now(zone).date()
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
        rise, sset = sun_times(ap.lat, ap.lon, noon, tz)
        t = rise.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        flags, wxs = [], []
        while t + timedelta(hours=1) <= sset:
            wx = fc.at(ap.lat, ap.lon, t.astimezone(timezone.utc))
            if wx is not None:
                wxs.append(wx)
                flags.append((t, _hour_ok(wx, mins)))
            t += timedelta(hours=1)
        all_day = [fc.at(ap.lat, ap.lon, datetime(day.year, day.month, day.day, h, tzinfo=zone).astimezone(timezone.utc))
                   for h in range(0, 24, 3)]
        all_day = [w for w in all_day if w]
        if not wxs:
            out.append(DayOutlook(day, "No data", 0, 0, "—", None, None, 0, 0, 0, "—", None, None, "—", None,
                                  "Beyond the forecast range"))
            continue
        cigs = [w.ceiling_agl_ft() for w in wxs if w.ceiling_agl_ft() is not None]
        viss = [w.vis_sm for w in wxs if w.vis_sm is not None]
        flyable = sum(ok for _, ok in flags)
        storm_lvl = ("likely" if any(w.thunder or (w.cape >= mins.cape_nogo_jkg and w.precip_prob >= 30) for w in wxs)
                     else "some" if any(w.cape >= mins.cape_caution_jkg and w.precip_prob >= 30 for w in wxs)
                     else "low")
        verdict = "Good" if flyable >= 6 else "Fair" if flyable >= 3 else "Poor"
        mid = min(wxs, key=lambda w: abs((w.time.astimezone(zone).hour) - 12))
        wd, ws, _, _ = mid.wind_at(6000)
        temps = [w.temp_c for w in all_day + wxs if w.temp_c is not None]
        fz = [w.freezing_level_ft for w in wxs if w.freezing_level_ft is not None]
        rain = max(w.precip_prob for w in wxs)
        low_cig = min(cigs) if cigs else None
        bits = []
        if low_cig is not None:
            bits.append(f"ceilings down to ~{round(low_cig, -2):,.0f} ft")
        if storm_lvl != "low":
            bits.append(f"storms {storm_lvl}")
        if rain >= 50:
            bits.append(f"rain {rain:.0f}%")
        gust = max(w.gust_kt for w in wxs)
        if gust >= 20:
            bits.append(f"gusts {gust:.0f} kt")
        out.append(DayOutlook(
            day, verdict, flyable, len(flags), _windows(flags), low_cig, min(viss) if viss else None,
            max(w.wind_kt for w in wxs), gust, rain, storm_lvl,
            max(temps) if temps else None, min(temps) if temps else None, f"{wd:03.0f}@{ws:.0f}",
            min(fz) if fz else None, ", ".join(bits) or "no significant weather"))
    return out


def hourly_rows(fc: GridForecast, ap: Airport, day: date, tz: str = "America/New_York") -> list[dict]:
    zone = ZoneInfo(tz)
    rows = []
    for h in range(5, 22):
        t = datetime(day.year, day.month, day.day, h, tzinfo=zone)
        wx = fc.at(ap.lat, ap.lon, t.astimezone(timezone.utc))
        if wx is None:
            continue
        cig = wx.ceiling_agl_ft()
        d3, s3, _, _ = wx.wind_at(ap.elev_ft + 3000)
        d6, s6, _, _ = wx.wind_at(6000)
        d9, s9, _, _ = wx.wind_at(9000)
        rows.append({
            "Time": t.strftime("%-I %p"),
            "Category (est.)": flight_category(cig, wx.vis_sm),
            "Conditions": wx.wx_text,
            "Ceiling AGL": "none" if cig is None else f"{round(cig, -2):,.0f}",
            "Vis": "—" if wx.vis_sm is None else ("10+" if wx.vis_sm >= 10 else f"{wx.vis_sm:.0f}"),
            "Wind": f"{wx.wind_dir:03.0f}@{wx.wind_kt:.0f}" + (f"G{wx.gust_kt:.0f}" if wx.gust_kt >= wx.wind_kt + 5 else ""),
            "Rain %": f"{wx.precip_prob:.0f}",
            "CAPE": f"{wx.cape:.0f}",
            "Temp °F": "—" if wx.temp_c is None else f"{wx.temp_c * 9 / 5 + 32:.0f}",
            "3000 AGL": f"{d3:03.0f}@{s3:.0f}",
            "6000": f"{d6:03.0f}@{s6:.0f}",
            "9000": f"{d9:03.0f}@{s9:.0f}",
        })
    return rows


def route_outlook(fc: GridForecast, legs, mins: Minimums, tz: str = "America/New_York",
                  days: int = 10, start: date | None = None) -> list[dict]:
    """Worst midday (10 AM–4 PM) en-route conditions per day along a route."""
    zone = ZoneInfo(tz)
    start = start or datetime.now(zone).date()
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        worst_margin, worst_where, rain, storms, n = None, "", 0.0, "low", 0
        for leg in legs:
            clearance = mins.mountain_terrain_clearance_ft if leg.mountainous else mins.terrain_clearance_ft
            for la, lo, d in leg.samples:
                for h in (10, 13, 16):
                    t = datetime(day.year, day.month, day.day, h, tzinfo=zone).astimezone(timezone.utc)
                    wx = fc.at(la, lo, t)
                    if wx is None:
                        continue
                    n += 1
                    cig = wx.ceiling_agl_ft()
                    terr = leg.max_terrain(d - 10, d + 10)
                    if cig is not None:
                        margin = wx.surface_elev_ft + cig - (terr + clearance + mins.cloud_clearance_below_ft)
                        if worst_margin is None or margin < worst_margin:
                            worst_margin, worst_where = margin, f"{la:.1f},{lo:.1f}"
                    rain = max(rain, wx.precip_prob)
                    if wx.thunder or (wx.cape >= mins.cape_nogo_jkg and wx.precip_prob >= 30):
                        storms = "likely"
                    elif storms == "low" and wx.cape >= mins.cape_caution_jkg and wx.precip_prob >= 30:
                        storms = "some"
        if n == 0:
            rows.append({"day": day, "verdict": "No data", "ceiling_margin": None, "rain": None, "storms": "—"})
            continue
        if storms == "likely" or (worst_margin is not None and worst_margin < 0):
            verdict = "Poor"
        elif storms == "some" or rain >= 50 or (worst_margin is not None and worst_margin < 1000):
            verdict = "Fair"
        else:
            verdict = "Good"
        rows.append({"day": day, "verdict": verdict, "ceiling_margin": worst_margin, "rain": rain,
                     "storms": storms})
    return rows
