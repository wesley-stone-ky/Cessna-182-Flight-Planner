"""7-day outlook: daily flyability per airport and along a route."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .airports import Airport, AirportDB, best_runway
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
    reasons: list[str] = field(default_factory=list)   # plain-English "why", most important first
    source: str = ""                                    # NWS forecast / model estimate / mixed
    covered_hours: int = 0
    hours: list["HourCheck"] = field(default_factory=list)


@dataclass
class HourCheck:
    time: datetime
    ok: bool
    problems: list[tuple[str, str]]   # (kind, text)
    source: str                       # "NWS" or "model"
    ceiling_ft: float | None
    vis_sm: float | None
    wind: str
    rain_pct: float
    storms: str


def source_of(wx: WxPoint) -> str:
    return "NWS" if wx.nws else "model"


def check_hour(wx: WxPoint, mins: Minimums, ap: Airport | None = None) -> list[tuple[str, str]]:
    """Which of your airport minimums this hour breaks, with the numbers.

    With `ap`, also the crosswind on its best runway, using the gust value (same rule as the trip planner)."""
    out = []
    cig = wx.ceiling_agl_ft()
    if cig is not None and cig < mins.airport_min_ceiling_ft_agl:
        out.append(("ceiling", f"ceiling ~{round(cig, -2):,.0f} ft (min {mins.airport_min_ceiling_ft_agl:,.0f})"))
    if wx.vis_sm is not None and wx.vis_sm < mins.airport_min_vis_sm:
        out.append(("visibility", f"visibility {wx.vis_sm:.1f} sm (min {mins.airport_min_vis_sm:.0f})"))
    if wx.gust_kt >= mins.max_gust_kt:
        out.append(("gusts", f"gusts {wx.gust_kt:.0f} kt (limit {mins.max_gust_kt:.0f})"))
    elif wx.wind_kt >= mins.max_surface_wind_kt:
        out.append(("wind", f"wind {wx.wind_kt:.0f} kt (limit {mins.max_surface_wind_kt:.0f})"))
    if ap is not None:
        kt = max(wx.wind_kt, wx.gust_kt)
        if not ap.runways:
            if kt > mins.max_crosswind_kt:
                out.append(("crosswind", f"wind {kt:.0f} kt and no runway data to check the crosswind"))
        else:
            rw = best_runway(ap, wx.wind_dir, kt)
            if rw is None:
                out.append(("crosswind", f"no runway of 2,500 ft or more without a tailwind (wind "
                                         f"{wx.wind_dir:03.0f}@{kt:.0f})"))
            elif rw[1] > mins.max_crosswind_kt:
                out.append(("crosswind", f"crosswind {rw[1]:.0f} kt incl. gusts on rwy {rw[0]} "
                                         f"(limit {mins.max_crosswind_kt:.0f})"))
    if wx.storm_level(mins.cape_caution_jkg, mins.cape_nogo_jkg) == "likely":
        out.append(("storms", f"thunderstorms likely ({wx.storm_detail()})"))
    return out


def _hour_ok(wx: WxPoint, mins: Minimums) -> bool:
    return not check_hour(wx, mins)


KIND_LABEL = {"ceiling": "Low ceilings", "visibility": "Low visibility", "gusts": "Gusty winds",
              "wind": "Strong surface wind", "crosswind": "Crosswind", "storms": "Thunderstorms"}


def _span(times: list[datetime]) -> str:
    if not times:
        return ""
    a, b = min(times), max(times) + timedelta(hours=1)
    return f"{a:%-I %p}–{b:%-I %p}"


def explain_hours(hours: list[HourCheck], daylight: int, mins: Minimums) -> list[str]:
    """Roll hour-by-hour problems up into a few plain sentences."""
    lines = []
    covered = len(hours)
    if covered < daylight:
        lines.append(f"Forecast only covers {covered} of {daylight} daylight hours — "
                     "the rest of the day is past the end of the forecast, so this day is judged on part of it.")
    by_kind: dict[str, list[HourCheck]] = {}
    for h in hours:
        for kind, _ in h.problems:
            by_kind.setdefault(kind, []).append(h)
    for kind, hs in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        texts = [t for h in hs for k, t in h.problems if k == kind]
        if kind == "ceiling":
            worst = min(h.ceiling_ft for h in hs if h.ceiling_ft is not None)
            detail = f"lowest ~{round(worst, -2):,.0f} ft vs your {mins.airport_min_ceiling_ft_agl:,.0f} ft"
        elif kind == "visibility":
            worst = min(h.vis_sm for h in hs if h.vis_sm is not None)
            detail = f"down to {worst:.1f} sm vs your {mins.airport_min_vis_sm:.0f} sm"
        else:
            detail = texts[0]
        srcs = {h.source for h in hs}
        src = "NWS forecast" if srcs == {"NWS"} else "model estimate" if srcs == {"model"} else "NWS + model"
        lines.append(f"{KIND_LABEL.get(kind, kind)}: {len(hs)} of {covered} hours ({_span([h.time for h in hs])}), "
                     f"{detail} — {src}")
    return lines


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
                    days: int = 7, start: date | None = None) -> list[DayOutlook]:
    zone = ZoneInfo(tz)
    start = start or datetime.now(zone).date()
    out = []
    for i in range(days):
        day = start + timedelta(days=i)
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
        rise, sset = sun_times(ap.lat, ap.lon, noon, tz)
        t = rise.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        flags, wxs, checks, daylight = [], [], [], 0
        while t + timedelta(hours=1) <= sset:
            daylight += 1
            wx = fc.at(ap.lat, ap.lon, t.astimezone(timezone.utc))
            if wx is not None:
                probs = check_hour(wx, mins, ap)
                wxs.append(wx)
                flags.append((t, not probs))
                checks.append(HourCheck(
                    t, not probs, probs, source_of(wx), wx.ceiling_agl_ft(), wx.vis_sm,
                    f"{wx.wind_dir:03.0f}@{wx.wind_kt:.0f}" + (f"G{wx.gust_kt:.0f}" if wx.gust_kt >= wx.wind_kt + 5 else ""),
                    wx.precip_prob, wx.storm_level(mins.cape_caution_jkg, mins.cape_nogo_jkg)))
            t += timedelta(hours=1)
        all_day = [fc.at(ap.lat, ap.lon, datetime(day.year, day.month, day.day, h, tzinfo=zone).astimezone(timezone.utc))
                   for h in range(0, 24, 3)]
        all_day = [w for w in all_day if w]
        if not wxs:
            out.append(DayOutlook(day, "No data", 0, daylight, "—", None, None, 0, 0, 0, "—", None, None, "—", None,
                                  "Beyond the forecast range",
                                  ["No forecast reaches this day yet (the NWS forecast covers 7 days)."]))
            continue
        cigs = [w.ceiling_agl_ft() for w in wxs if w.ceiling_agl_ft() is not None]
        viss = [w.vis_sm for w in wxs if w.vis_sm is not None]
        flyable = sum(ok for _, ok in flags)
        lv = {w.storm_level(mins.cape_caution_jkg, mins.cape_nogo_jkg) for w in wxs}
        storm_lvl = "likely" if "likely" in lv else "some" if "some" in lv else "low"
        covered = len(flags)
        frac = flyable / covered
        verdict = "Good" if frac >= 0.6 and flyable >= 3 else "Fair" if frac >= 0.3 else "Poor"
        if covered < 0.5 * daylight:
            verdict = "Partial"   # don't call a day Poor just because the forecast ends mid-day
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
            max(temps) if temps else None, min(temps) if temps else None,
            f"{wd:03.0f}@{ws:.0f}" + (" (est.)" if mid.aloft_estimated else ""),
            min(fz) if fz else None, ", ".join(bits) or "no significant weather",
            explain_hours(checks, daylight, mins) or ["Every forecast daylight hour meets your airport minimums."],
            {"NWS"} == {c.source for c in checks} and "NWS forecast" or
            ({"model"} == {c.source for c in checks} and "model estimate" or "NWS + model"),
            covered, checks))
    return out


@lru_cache(maxsize=4096)
def _place(lat: float, lon: float) -> str:
    db = AirportDB.load()
    best = min(db.airports.values(), key=lambda a: (a.lat - lat) ** 2 + (a.lon - lon) ** 2)
    return f"near {best.ident} ({best.city}, {best.state})"


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
            "Thunder %": "—" if wx.thunder_prob is None else f"{wx.thunder_prob:.0f}",
            "Temp °F": "—" if wx.temp_c is None else f"{wx.temp_c * 9 / 5 + 32:.0f}",
            "3000 AGL": f"{d3:03.0f}@{s3:.0f}",
            "6000": f"{d6:03.0f}@{s6:.0f}",
            "9000": f"{d9:03.0f}@{s9:.0f}",
        })
    return rows


def route_outlook(fc: GridForecast, legs, mins: Minimums, tz: str = "America/New_York",
                  days: int = 7, start: date | None = None) -> list[dict]:
    """Worst midday (10 AM–4 PM) en-route conditions per day along a route."""
    zone = ZoneInfo(tz)
    start = start or datetime.now(zone).date()
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        worst_margin, worst_where, rain, storms, n = None, "", 0.0, "low", 0
        spots: list[tuple] = []   # (kind, text, time, place, source, severity: lower = worse)
        srcs = set()
        for leg in legs:
            clearance = mins.mountain_terrain_clearance_ft if leg.mountainous else mins.terrain_clearance_ft
            for la, lo, d in leg.samples:
                for h in (10, 13, 16):
                    t = datetime(day.year, day.month, day.day, h, tzinfo=zone).astimezone(timezone.utc)
                    wx = fc.at(la, lo, t)
                    if wx is None:
                        continue
                    n += 1
                    srcs.add(source_of(wx))
                    cig = wx.ceiling_agl_ft()
                    terr = leg.max_terrain(d - 10, d + 10)
                    lt = datetime(day.year, day.month, day.day, h, tzinfo=zone)
                    if cig is not None:
                        margin = wx.surface_elev_ft + cig - (terr + clearance + mins.cloud_clearance_below_ft)
                        if worst_margin is None or margin < worst_margin:
                            worst_margin, worst_where = margin, f"{la:.1f},{lo:.1f}"
                        if margin < 1000:
                            spots.append(("room", f"cloud base ~{wx.surface_elev_ft + cig:,.0f} ft MSL over terrain "
                                          f"{terr:,.0f} ft ({margin:+,.0f} ft of room after your "
                                          f"{clearance:,.0f} ft clearance + 500 ft below clouds)",
                                          lt, _place(round(la, 2), round(lo, 2)), source_of(wx), margin))
                    rain = max(rain, wx.precip_prob)
                    lvl = wx.storm_level(mins.cape_caution_jkg, mins.cape_nogo_jkg)
                    if lvl != "low":
                        spots.append(("storms" if lvl == "likely" else "storms?",
                                      f"thunderstorms {lvl} ({wx.storm_detail()})",
                                      lt, _place(round(la, 2), round(lo, 2)), source_of(wx),
                                      -(wx.thunder_prob or 0) - wx.cape / 100))
                    elif wx.precip_prob >= 50 and wx.wcode >= 51:
                        spots.append(("rain", f"rain likely ({wx.precip_prob:.0f}%)", lt,
                                      _place(round(la, 2), round(lo, 2)), source_of(wx), -wx.precip_prob))
                    if lvl == "likely":
                        storms = "likely"
                    elif storms == "low" and lvl == "some":
                        storms = "some"
        if n == 0:
            rows.append({"day": day, "verdict": "No data", "ceiling_margin": None, "rain": None, "storms": "—",
                         "reasons": ["No forecast reaches this day yet."], "source": ""})
            continue
        if storms == "likely" or (worst_margin is not None and worst_margin < 0):
            verdict = "Poor"
        elif storms == "some" or rain >= 50 or (worst_margin is not None and worst_margin < 1000):
            verdict = "Fair"
        else:
            verdict = "Good"
        rows.append({"day": day, "verdict": verdict, "ceiling_margin": worst_margin, "rain": rain,
                     "storms": storms, "reasons": _route_reasons(spots, n, len(legs)),
                     "source": "NWS forecast" if srcs == {"NWS"} else "model estimate" if srcs == {"model"}
                     else "NWS + model"})
    return rows


def _route_reasons(spots, n_checks: int, n_legs: int) -> list[str]:
    """Group en-route problem spots by kind; show where and when the worst one is."""
    if not spots:
        return ["Nothing along the route breaks your minimums at 10 AM, 1 PM or 4 PM."]
    order = {"storms": 0, "room": 1, "storms?": 2, "rain": 3}
    label = {"storms": "Thunderstorms likely", "room": "Not much room under the clouds over the terrain",
             "storms?": "Possible thunderstorms", "rain": "Rain likely"}
    out = []
    kinds: dict[str, list] = {}
    for sp in spots:
        kinds.setdefault(sp[0], []).append(sp)
    for kind in sorted(kinds, key=lambda k: order.get(k, 9)):
        sps = kinds[kind]
        first = min(sps, key=lambda s_: s_[5])
        places = sorted({s_[3] for s_ in sps})
        times = sorted({s_[2] for s_ in sps})
        when = ", ".join(t.strftime("%-I %p") for t in times)
        srcs = {s_[4] for s_ in sps}
        src = "NWS forecast" if srcs == {"NWS"} else "model estimate" if srcs == {"model"} else "NWS + model"
        out.append(f"{label[kind]}: worst {first[3]} at {first[2]:%-I %p} — {first[1]}; "
                   f"{len(places)} spot{'s' if len(places) != 1 else ''} affected ({when}) — {src}")
    return out
