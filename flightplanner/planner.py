"""Route building, wind-corrected leg simulation and go/no-go screening."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import cached_property
from zoneinfo import ZoneInfo

from .aircraft import Aircraft
from .airports import Airport, AirportDB, best_runway
from .config import Minimums
from .geo import (haversine_nm, initial_bearing, magnetic_variation, sample_line,
                  true_to_magnetic, wind_components, wind_triangle)
from .sun import sun_times
from .terrain import Terrain
from .weather import STD_LAPSE_C_PER_FT, Advisory, GridForecast, WxPoint, taf_at

NOGO, CAUTION, INFO = "NO-GO", "CAUTION", "INFO"
STATUS_RANK = {"GO": 0, "MARGINAL": 1, "NO-GO": 2}
WX_STEP_NM = 20.0
FREEZING_PRECIP = {56, 57, 66, 67}
IGNORED_HAZARDS = {"FZLVL", "TURB-HI"}  # freezing-level contours / jet-altitude turbulence


# ---------------------------------------------------------------------------
# Route structures
# ---------------------------------------------------------------------------
@dataclass
class Leg:
    dep: Airport
    arr: Airport
    terrain: list[tuple[float, float]] = field(default_factory=list)  # (dist, max ft)

    @cached_property
    def dist_nm(self) -> float:
        return haversine_nm(self.dep.lat, self.dep.lon, self.arr.lat, self.arr.lon)

    @cached_property
    def true_course(self) -> float:
        return initial_bearing(self.dep.lat, self.dep.lon, self.arr.lat, self.arr.lon)

    @cached_property
    def mag_course(self) -> float:
        mid_lat = (self.dep.lat + self.arr.lat) / 2
        mid_lon = (self.dep.lon + self.arr.lon) / 2
        return true_to_magnetic(self.true_course, mid_lat, mid_lon)

    @cached_property
    def samples(self) -> list[tuple[float, float, float]]:
        return sample_line(self.dep.lat, self.dep.lon, self.arr.lat, self.arr.lon, WX_STEP_NM)

    def max_terrain(self, d0: float = 0.0, d1: float | None = None) -> float:
        d1 = self.dist_nm if d1 is None else d1
        vals = [h for d, h in self.terrain if d0 - 3 <= d <= d1 + 3]
        if not vals:
            vals = [h for _, h in self.terrain] or [max(self.dep.elev_ft, self.arr.elev_ft)]
        return max(vals)

    @property
    def mountainous(self) -> bool:
        return self.max_terrain() > 3000


@dataclass
class RoutePlan:
    airports: list[Airport]
    legs: list[Leg]

    @property
    def stops(self) -> list[Airport]:
        return self.airports[1:-1]

    @property
    def label(self) -> str:
        if not self.stops:
            return "Nonstop"
        return "via " + ", ".join(a.ident for a in self.stops)

    @property
    def total_nm(self) -> float:
        return sum(l.dist_nm for l in self.legs)

    @property
    def route_string(self) -> str:
        return " ".join(a.ident for a in self.airports)


@dataclass
class Finding:
    level: str
    leg: int
    where: str
    text: str
    kind: str = ""


@dataclass
class ProfilePoint:
    dist_nm: float
    alt_ft: float
    terrain_ft: float
    ceiling_msl_ft: float | None
    eta: datetime


@dataclass
class AirportWx:
    ident: str
    when: datetime
    source: str
    ceiling_ft: float | None
    vis_sm: float | None
    wind_dir: float | None
    wind_kt: float | None
    gust_kt: float | None
    wx: str
    best_runway: str = ""
    crosswind_kt: float | None = None
    headwind_kt: float | None = None
    gust_spread_kt: float = 0.0
    xwind_unchecked: str = ""      # why the crosswind could not be checked, if it couldn't
    wind_window_min: float = 0     # wind values are the worst within +/- this many minutes

    def summary(self) -> str:
        cig = "no ceiling" if self.ceiling_ft is None else f"ceiling ~{int(round(self.ceiling_ft, -2)):,} ft"
        vis = "vis ?" if self.vis_sm is None else f"vis {min(self.vis_sm, 10):.0f}+ sm" if self.vis_sm >= 10 else f"vis {self.vis_sm:.1f} sm"
        wind = ""
        if self.wind_kt is not None:
            wd = "VRB" if self.wind_dir is None else f"{int(self.wind_dir):03d}"
            wind = f", wind {wd}@{self.wind_kt:.0f}" + (f"G{self.gust_kt:.0f}" if self.gust_kt and self.gust_kt > (self.wind_kt or 0) + 4 else "")
        xw = f", rwy {self.best_runway} xwind {self.crosswind_kt:.0f} kt incl. gusts" if self.best_runway and self.crosswind_kt is not None else ""
        win = f", worst wind within ±{self.wind_window_min:.0f} min" if self.wind_window_min else ""
        wx = f", {self.wx}" if self.wx else ""
        return f"{cig}, {vis}{wind}{xw}{wx} ({self.source}{win})"


@dataclass
class LegResult:
    leg: Leg
    index: int
    dep_time: datetime
    arr_time: datetime
    alt_ft: float
    minutes: float
    fuel_used_gal: float
    fuel_at_landing_gal: float
    reserve_required_gal: float
    avg_gs_kt: float
    avg_wind_component_kt: float   # + tailwind / - headwind
    mag_heading: float
    profile: list[ProfilePoint]
    dep_wx: AirportWx | None
    arr_wx: AirportWx | None
    findings: list[Finding]


@dataclass
class PlanResult:
    plan: RoutePlan
    dep_time: datetime
    arr_time: datetime
    legs: list[LegResult]
    findings: list[Finding]
    confidence: str

    @property
    def status(self) -> str:
        if any(f.level == NOGO for f in self.findings):
            return "NO-GO"
        if any(f.level == CAUTION for f in self.findings):
            return "MARGINAL"
        return "GO"

    @property
    def elapsed_hours(self) -> float:
        return (self.arr_time - self.dep_time).total_seconds() / 3600

    @property
    def flight_hours(self) -> float:
        return sum(l.minutes for l in self.legs) / 60

    @property
    def score(self) -> float:
        n_nogo = sum(f.level == NOGO for f in self.findings)
        n_caut = sum(f.level == CAUTION for f in self.findings)
        return (STATUS_RANK[self.status] * 1000 + n_nogo * 50 + n_caut * 25
                + self.elapsed_hours * 12 + len(self.plan.stops) * 15)

    def top_reasons(self, n: int = 3) -> list[str]:
        order = sorted(self.findings, key=lambda f: 0 if f.level == NOGO else 1 if f.level == CAUTION else 2)
        seen, out = set(), []
        for f in order:
            if f.level == INFO or f.text in seen:
                continue
            seen.add(f.text)
            out.append(f"{f.level}: {f.text}")
            if len(out) >= n:
                break
        return out


# ---------------------------------------------------------------------------
# Candidate routes
# ---------------------------------------------------------------------------
def _leg_ok(ac: Aircraft, mins: Minimums, a: Airport, b: Airport) -> bool:
    d = haversine_nm(a.lat, a.lon, b.lat, b.lon)
    hours, gal = ac.still_air_leg(d, a.elev_ft, b.elev_ft, 6500, mins.planning_headwind_kt)
    reserve = mins.reserve_minutes / 60 * ac.cruise_gph
    return gal + reserve <= ac.usable_gal and hours <= mins.max_leg_hours + 0.01


def _sells_avgas(ap: Airport) -> bool:
    """Never route a fuel stop to an airport the FAA data says has no 100LL."""
    return ap.avgas is not False


def _stop_cost(ap: Airport, mins: Minimums) -> float:
    cost = 0.0
    if mins.prefer_known_fuel_stops and not ap.known_fuel:
        cost += 45
    if ap.longest_rwy_ft < 4000:
        cost += 10
    if not ap.lighted:
        cost += 10
    if ap.type == "large_airport":
        cost += 40  # Class B/C hassle, ramp fees
    return cost


def _diverse(cands: list[tuple[float, Airport]], n: int, min_sep_nm: float = 30) -> list[Airport]:
    picked: list[Airport] = []
    for _, ap in sorted(cands, key=lambda c: c[0]):
        if all(haversine_nm(ap.lat, ap.lon, p.lat, p.lon) >= min_sep_nm for p in picked):
            picked.append(ap)
        if len(picked) >= n:
            break
    return picked


def candidate_plans(db: AirportDB, dep: Airport, dest: Airport, ac: Aircraft, mins: Minimums,
                    max_one_stop: int = 6, max_two_stop: int = 4) -> list[RoutePlan]:
    D = haversine_nm(dep.lat, dep.lon, dest.lat, dest.lon)
    plans: list[RoutePlan] = []
    nonstop_ok = _leg_ok(ac, mins, dep, dest)
    if nonstop_ok:
        plans.append(RoutePlan([dep, dest], [Leg(dep, dest)]))

    # One-stop options (needed for range, or as weather/terrain alternatives)
    xt_max = max(40.0, 0.25 * D)
    scored: list[tuple[float, Airport]] = []
    for ap, xt, at in db.corridor(dep, dest, xt_max, mins.min_runway_ft, 0.15, 0.85):
        if not _sells_avgas(ap):
            continue
        if not (_leg_ok(ac, mins, dep, ap) and _leg_ok(ac, mins, ap, dest)):
            continue
        d1 = haversine_nm(dep.lat, dep.lon, ap.lat, ap.lon)
        d2 = haversine_nm(ap.lat, ap.lon, dest.lat, dest.lon)
        detour = d1 + d2 - D
        balance = abs(d1 - d2) / D * 60  # prefer stops near the middle
        scored.append((detour + balance + _stop_cost(ap, mins), ap))
    for ap in _diverse(scored, max_one_stop if not nonstop_ok else max(3, max_one_stop - 2)):
        plans.append(RoutePlan([dep, ap, dest], [Leg(dep, ap), Leg(ap, dest)]))

    # Two-stop options only if nothing with <=1 stop works
    if not plans:
        firsts = [(ap, xt, at) for ap, xt, at in db.corridor(dep, dest, xt_max, mins.min_runway_ft, 0.15, 0.55)
                  if _leg_ok(ac, mins, dep, ap) and _sells_avgas(ap)]
        seconds = [(ap, xt, at) for ap, xt, at in db.corridor(dep, dest, xt_max, mins.min_runway_ft, 0.45, 0.9)
                   if _leg_ok(ac, mins, ap, dest) and _sells_avgas(ap)]
        firsts = sorted(firsts, key=lambda c: _stop_cost(c[0], mins) + abs(c[1]))[:25]
        seconds = sorted(seconds, key=lambda c: _stop_cost(c[0], mins) + abs(c[1]))[:25]
        pairs: list[tuple[float, Airport, Airport]] = []
        for a, _, at1 in firsts:
            for b, _, at2 in seconds:
                if at2 <= at1 + 40 or not _leg_ok(ac, mins, a, b):
                    continue
                total = (haversine_nm(dep.lat, dep.lon, a.lat, a.lon) + haversine_nm(a.lat, a.lon, b.lat, b.lon)
                         + haversine_nm(b.lat, b.lon, dest.lat, dest.lon))
                pairs.append((total - D + _stop_cost(a, mins) + _stop_cost(b, mins), a, b))
        pairs.sort(key=lambda p: p[0])
        used: list[tuple[Airport, Airport]] = []
        for _, a, b in pairs:
            if any(haversine_nm(a.lat, a.lon, u.lat, u.lon) < 30 and haversine_nm(b.lat, b.lon, v.lat, v.lon) < 30
                   for u, v in used):
                continue
            used.append((a, b))
            plans.append(RoutePlan([dep, a, b, dest], [Leg(dep, a), Leg(a, b), Leg(b, dest)]))
            if len(used) >= max_two_stop:
                break
    return plans


def attach_terrain(plans: list[RoutePlan], terrain: Terrain, mins: Minimums) -> None:
    for p in plans:
        for leg in p.legs:
            if not leg.terrain:
                leg.terrain = terrain.leg_profile(leg.dep.lat, leg.dep.lon, leg.arr.lat, leg.arr.lon,
                                                  mins.corridor_halfwidth_nm)


def forecast_points(plans: list[RoutePlan]) -> list[tuple[float, float]]:
    pts = []
    for p in plans:
        for leg in p.legs:
            pts += [(la, lo) for la, lo, _ in leg.samples]
    return pts


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
@dataclass
class Context:
    ac: Aircraft
    mins: Minimums
    forecast: GridForecast
    db: AirportDB
    tafs: dict[str, dict] = field(default_factory=dict)
    advisories: list[Advisory] = field(default_factory=list)
    tz: str = "America/New_York"
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    wb: object | None = None        # flightplanner.wb.WeightBalance
    loading: object | None = None   # flightplanner.wb.Loading (people/bags); fuel = ac.usable_gal

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(ZoneInfo(self.tz))

    def fmt(self, dt: datetime) -> str:
        return self.local(dt).strftime("%a %-I:%M %p")


def hemispheric_altitudes(mag_course: float, ifr: bool, max_alt: float) -> list[float]:
    """14 CFR 91.159 (VFR) / 91.179 (IFR) cruising altitudes."""
    east = 0 <= mag_course < 180
    base = [3000, 5000, 7000, 9000, 11000] if east else [4000, 6000, 8000, 10000, 12000]
    alts = [a if ifr else a + 500 for a in base]
    return [a for a in alts if a <= max_alt]


_NEAR_CACHE: dict[tuple[float, float], str] = {}


def _near(ctx: Context, lat: float, lon: float) -> str:
    key = (round(lat, 2), round(lon, 2))
    if key not in _NEAR_CACHE:
        _NEAR_CACHE[key] = _near_uncached(ctx, lat, lon)
    return _NEAR_CACHE[key]


def _near_uncached(ctx: Context, lat: float, lon: float) -> str:
    best, bd = None, 1e9
    for ap in ctx.db.airports.values():
        d = abs(ap.lat - lat) + abs(ap.lon - lon)
        if d < bd:
            best, bd = ap, d
    return f"near {best.ident} ({best.city}, {best.state})" if best else f"{lat:.2f},{lon:.2f}"


def _simulate(ctx: Context, leg: Leg, dep_time: datetime, alt: float):
    """Fly the leg at `alt`; return timing/fuel and per-sample ETAs."""
    ac = ctx.ac
    climb = ac.climb(leg.dep.elev_ft, alt)
    dsc = ac.descent(alt, leg.arr.elev_ft)

    def gs_at(lat, lon, when, tas, alt_ft):
        wx = ctx.forecast.at(lat, lon, when)
        if wx is None:
            return tas, 0.0, 0.0, None
        wdir, wspd, _, _ = wx.wind_at(alt_ft)
        gs, wca = wind_triangle(leg.true_course, tas, wdir, wspd)
        if wx.aloft_estimated:
            # No real winds aloft (NWS grids): no tailwind credit, and at least the planning headwind.
            gs = min(gs, tas - ctx.mins.planning_headwind_kt)
        return gs, wca, gs - tas, wx

    # climb
    d0 = leg.samples[0]
    gs_c, _, _, _ = gs_at(d0[0], d0[1], dep_time, ac.climb_speed_kt(), (leg.dep.elev_ft + alt) / 2)
    climb_d = climb.minutes / 60 * gs_c
    # descent (use destination-area winds at arrival-ish time; refined below)
    dl = leg.samples[-1]
    t_guess = dep_time + timedelta(hours=leg.dist_nm / max(60.0, ac.ktas(alt)))
    gs_d, _, _, _ = gs_at(dl[0], dl[1], t_guess, ac.ktas(alt) + 5, (leg.arr.elev_ft + alt) / 2)
    dsc_d = dsc.minutes / 60 * gs_d
    if climb_d + dsc_d > leg.dist_nm:
        scale = leg.dist_nm / (climb_d + dsc_d)
        climb_d *= scale
        dsc_d *= scale
    cruise_start, cruise_end = climb_d, leg.dist_nm - dsc_d

    t = dep_time + timedelta(minutes=climb.minutes)
    cruise_h = 0.0
    etas: list[datetime] = []
    gss, wcas, comps = [], [], []
    tas = ac.ktas(alt)
    prev_d = 0.0
    for la, lo, d in leg.samples:
        if d <= cruise_start:
            etas.append(dep_time + timedelta(minutes=climb.minutes * (d / max(cruise_start, 1e-6))))
            prev_d = d
            continue
        seg_lo, seg_hi = max(prev_d, cruise_start), min(d, cruise_end)
        if seg_hi > seg_lo:
            gs, wca, comp, _ = gs_at(la, lo, t, tas, alt)
            h = (seg_hi - seg_lo) / gs
            cruise_h += h
            t += timedelta(hours=h)
            gss.append((gs, seg_hi - seg_lo))
            wcas.append(wca)
            comps.append((comp, seg_hi - seg_lo))
        etas.append(t)
        prev_d = d
    arr_time = t + timedelta(minutes=dsc.minutes)
    etas[-1] = arr_time
    fuel = ac.taxi_gal + climb.gal + cruise_h * ac.cruise_gph + dsc.gal
    minutes = climb.minutes + cruise_h * 60 + dsc.minutes
    wsum = sum(w for _, w in gss) or 1
    avg_gs = sum(g * w for g, w in gss) / wsum if gss else tas
    avg_comp = sum(c * w for c, w in comps) / wsum if comps else 0.0
    avg_wca = sum(wcas) / len(wcas) if wcas else 0.0
    return dict(arr_time=arr_time, fuel=fuel, minutes=minutes, etas=etas, avg_gs=avg_gs,
                avg_comp=avg_comp, wca=avg_wca, cruise=(cruise_start, cruise_end))


def _airport_wx(ctx: Context, ap: Airport, when: datetime) -> AirportWx | None:
    taf = ctx.tafs.get(ap.ident)
    prevailing, temps = taf_at(taf, when) if taf else (None, [])
    if prevailing is not None:
        src = "TAF"
        if taf.get("station") and taf.get("station") != ap.ident:
            src = f"TAF from {taf['station']}, {taf.get('dist_nm', 0):.0f} nm away"
        aw = AirportWx(ap.ident, when, src, prevailing.ceiling_ft, prevailing.vis_sm,
                       prevailing.wind_dir, prevailing.wind_kt, prevailing.gust_kt, prevailing.wx)
        for tp in temps:  # use the worse of prevailing / TEMPO for screening
            if tp.ceiling_ft is not None and (aw.ceiling_ft is None or tp.ceiling_ft < aw.ceiling_ft):
                aw.ceiling_ft = tp.ceiling_ft
                aw.source = src + " incl. TEMPO/PROB"
            if tp.vis_sm is not None and (aw.vis_sm is None or tp.vis_sm < aw.vis_sm):
                aw.vis_sm = tp.vis_sm
                aw.source = src + " incl. TEMPO/PROB"
            if tp.wx and tp.wx not in aw.wx:
                aw.wx = (aw.wx + " " + tp.wx).strip()
    else:
        wx = ctx.forecast.at(ap.lat, ap.lon, when)
        if wx is None:
            return None
        aw = AirportWx(ap.ident, when, "NWS forecast" if wx.nws else "model est.", wx.ceiling_agl_ft(), wx.vis_sm,
                       wx.wind_dir, wx.wind_kt, wx.gust_kt,
                       wx.wx_text if wx.wcode >= 45 else "")
    _apply_worst_wind(ctx, ap, aw)
    return aw


def _wind_samples(ctx: Context, ap: Airport, when: datetime) -> list[tuple[float | None, float, float]]:
    """(dir, kt, gust) for every TAF group (incl. TEMPO/PROB) or model hour within the wind window.

    Departures slip and arrivals run late, so a wind shift just after the ETA counts too."""
    taf = ctx.tafs.get(ap.ident)
    win = ctx.mins.wind_window_minutes
    out = []
    for k in range(-int(win // 30), int(win // 30) + 1):
        t = when + timedelta(minutes=30 * k)
        prevailing, temps = taf_at(taf, t) if taf else (None, [])
        if prevailing is not None:
            for p in [prevailing] + temps:
                if p.wind_kt is not None:
                    out.append((p.wind_dir, p.wind_kt, p.gust_kt or 0.0))
        else:
            wx = ctx.forecast.at(ap.lat, ap.lon, t)
            if wx is not None:
                out.append((wx.wind_dir, wx.wind_kt, wx.gust_kt or 0.0))
    return out


def _apply_worst_wind(ctx: Context, ap: Airport, aw: AirportWx) -> None:
    """Replace aw's wind with the worst case in the window; crosswind uses the gust value."""
    samples = _wind_samples(ctx, ap, aw.when)
    if not samples:
        return
    aw.wind_window_min = ctx.mins.wind_window_minutes
    aw.wind_kt = max(k for _, k, _ in samples)
    aw.gust_kt = max(g for _, _, g in samples) or None
    aw.gust_spread_kt = max((g - k for _, k, g in samples if g), default=0.0)
    if not ap.runways:
        aw.xwind_unchecked = "no runway data"
        return
    worst = None
    for d, k, g in samples:
        rw = best_runway(ap, d, max(k, g))
        if rw is None:
            aw.xwind_unchecked = "no runway of 2,500 ft or more without a tailwind"
            continue
        if worst is None or rw[1] > worst[1]:
            worst, aw.wind_dir = rw, d
    if worst:
        aw.best_runway, aw.crosswind_kt, aw.headwind_kt = worst


def _check_airport(ctx: Context, aw: AirportWx | None, li: int, role: str, out: list[Finding]) -> None:
    m = ctx.mins
    if aw is None:
        out.append(Finding(CAUTION, li, aw.ident if aw else role, f"No forecast available for {role}", "nodata"))
        return
    where = f"{aw.ident} ({role}, {ctx.fmt(aw.when)})"
    floor_cig, floor_vis = (500, 1.0) if m.ifr else (1000, 3.0)
    if aw.ceiling_ft is not None:
        if aw.ceiling_ft < floor_cig:
            out.append(Finding(NOGO, li, where, f"{aw.ident} ceiling ~{aw.ceiling_ft:,.0f} ft ({aw.source})", "cig"))
        elif aw.ceiling_ft < m.airport_min_ceiling_ft_agl:
            out.append(Finding(CAUTION, li, where, f"{aw.ident} ceiling ~{aw.ceiling_ft:,.0f} ft, below your "
                               f"{m.airport_min_ceiling_ft_agl:,.0f} ft minimum ({aw.source})", "cig"))
    if aw.vis_sm is not None:
        if aw.vis_sm < floor_vis:
            out.append(Finding(NOGO, li, where, f"{aw.ident} visibility {aw.vis_sm:.1f} sm ({aw.source})", "vis"))
        elif aw.vis_sm < m.airport_min_vis_sm:
            out.append(Finding(CAUTION, li, where, f"{aw.ident} visibility {aw.vis_sm:.1f} sm, below your "
                               f"{m.airport_min_vis_sm:.0f} sm minimum", "vis"))
    if aw.gust_kt and aw.gust_kt >= m.max_gust_kt:
        out.append(Finding(NOGO, li, where, f"{aw.ident} gusts {aw.gust_kt:.0f} kt", "wind"))
    elif aw.wind_kt and aw.wind_kt >= m.max_surface_wind_kt:
        out.append(Finding(CAUTION, li, where, f"{aw.ident} surface wind {aw.wind_kt:.0f} kt", "wind"))
    if aw.gust_spread_kt >= m.max_gust_spread_kt:
        out.append(Finding(CAUTION, li, where, f"{aw.ident} gusts {aw.gust_spread_kt:.0f} kt above the sustained "
                           "wind: expect a bumpy, gusty final; add half the gust factor to approach speed", "wind"))
    if aw.xwind_unchecked:
        out.append(Finding(CAUTION, li, where, f"{aw.ident} crosswind not checked ({aw.xwind_unchecked}); "
                           "work out the runway wind yourself", "xwind"))
    if aw.crosswind_kt is not None:
        if aw.crosswind_kt > ctx.ac.demo_xwind:
            out.append(Finding(NOGO, li, where, f"{aw.ident} crosswind {aw.crosswind_kt:.0f} kt exceeds the "
                               f"{ctx.ac.demo_xwind:.0f} kt demonstrated value", "xwind"))
        elif aw.crosswind_kt > m.max_crosswind_kt:
            out.append(Finding(CAUTION, li, where, f"{aw.ident} crosswind {aw.crosswind_kt:.0f} kt "
                               f"(rwy {aw.best_runway}) above your {m.max_crosswind_kt:.0f} kt limit", "xwind"))
    if "TS" in (aw.wx or "").upper():
        out.append(Finding(NOGO, li, where, f"{aw.ident} thunderstorms forecast ({aw.source})", "ts"))


def evaluate_leg(ctx: Context, leg: Leg, li: int, dep_time: datetime, fuel_on_board: float) -> LegResult:
    ac, m = ctx.ac, ctx.mins
    findings: list[Finding] = []
    clearance = m.mountain_terrain_clearance_ft if leg.mountainous else m.terrain_clearance_ft
    min_safe = leg.max_terrain() + clearance
    alts = hemispheric_altitudes(leg.mag_course, m.ifr, ac.max_alt_ft)
    usable_alts = [a for a in alts if a >= min_safe] or [alts[-1]]
    if not [a for a in alts if a >= min_safe]:
        findings.append(Finding(CAUTION, li, f"leg {leg.dep.ident}-{leg.arr.ident}",
                                f"No standard cruising altitude clears terrain by {clearance:,.0f} ft "
                                f"(max terrain ~{leg.max_terrain():,.0f} ft)", "terrain"))

    best = None
    for alt in usable_alts:
        sim = _simulate(ctx, leg, dep_time, alt)
        violations = []
        if not m.ifr:
            cs, ce = sim["cruise"]
            for (la, lo, d), eta in zip(leg.samples, sim["etas"]):
                if d < cs - 1 or d > ce + 1:
                    continue
                wx = ctx.forecast.at(la, lo, eta)
                if wx is None:
                    continue
                cig = wx.ceiling_agl_ft()
                if cig is not None and wx.surface_elev_ft + cig < alt + m.cloud_clearance_below_ft:
                    violations.append((d, la, lo, eta, wx.surface_elev_ft + cig))
        key = (len(violations), sim["minutes"])
        if best is None or key < best[0]:
            best = (key, alt, sim, violations)
    _, alt, sim, violations = best

    # Ceiling vs terrain: is there ANY legal altitude under the clouds?
    for d, la, lo, eta, cig_msl in violations:
        terr = leg.max_terrain(d - WX_STEP_NM / 2, d + WX_STEP_NM / 2)
        need = terr + clearance + m.cloud_clearance_below_ft
        lvl = NOGO if cig_msl < need else CAUTION
        txt = (f"Ceilings ~{cig_msl:,.0f} ft MSL {_near(ctx, la, lo)} around {ctx.fmt(eta)}; terrain "
               f"{terr:,.0f} ft" + (" — not enough room for VFR" if lvl == NOGO else
                                    f" — you would have to fly below {alt:,.0f} ft"))
        findings.append(Finding(lvl, li, _near(ctx, la, lo), txt, "cig-enroute"))

    first_wx = ctx.forecast.at(leg.samples[0][0], leg.samples[0][1], dep_time)
    if first_wx is not None and first_wx.aloft_estimated:
        findings.append(Finding(INFO, li, f"leg {leg.dep.ident}-{leg.arr.ident}",
                                "The NWS forecast has no winds aloft, so time and fuel assume no tailwind and at "
                                f"least a {m.planning_headwind_kt:.0f} kt headwind. Check winds aloft in your "
                                "briefing.", "winds"))

    # Enroute hazards along the chosen altitude
    cs, ce = sim["cruise"]
    for (la, lo, d), eta in zip(leg.samples, sim["etas"]):
        wx = ctx.forecast.at(la, lo, eta)
        where = _near(ctx, la, lo)
        if wx is None:
            findings.append(Finding(CAUTION, li, where, "Beyond the forecast horizon here", "nodata"))
            continue
        pt_alt = alt if cs - 1 <= d <= ce + 1 else max(leg.dep.elev_ft, leg.arr.elev_ft) + 1500
        storms = wx.storm_level(m.cape_caution_jkg, m.cape_nogo_jkg)
        if storms == "likely":
            findings.append(Finding(NOGO, li, where, f"Thunderstorms likely {where} ~{ctx.fmt(eta)} "
                                    f"({wx.storm_detail()})", "ts"))
        elif storms == "some":
            findings.append(Finding(CAUTION, li, where, f"Possible thunderstorms {where} ~{ctx.fmt(eta)} "
                                    f"({wx.storm_detail()})", "ts"))
        if wx.wcode in FREEZING_PRECIP:
            findings.append(Finding(NOGO, li, where, f"Freezing precipitation {where}", "ice"))
        if wx.precip_prob >= m.precip_prob_caution_pct and wx.wcode >= 51:
            findings.append(Finding(CAUTION, li, where, f"Rain likely ({wx.precip_prob:.0f}%) {where} "
                                    f"~{ctx.fmt(eta)}", "rain"))
        if wx.vis_sm is not None and not m.ifr:
            if wx.vis_sm < 3:
                findings.append(Finding(NOGO, li, where, f"Visibility ~{wx.vis_sm:.1f} sm {where}", "vis"))
            elif wx.vis_sm < m.enroute_min_vis_sm:
                findings.append(Finding(CAUTION, li, where, f"Visibility ~{wx.vis_sm:.1f} sm {where}", "vis"))
        _, _, t_alt, rh_alt = wx.wind_at(pt_alt)
        if wx.aloft_estimated:
            # No temperature or humidity aloft in the NWS grid: use the standard lapse rate from the
            # surface, and assume cloud at altitude whenever the sky is mostly covered or rain is likely.
            if wx.temp_c is not None:
                t_alt = wx.temp_c - (pt_alt - wx.surface_elev_ft) * STD_LAPSE_C_PER_FT
            cloudy = (wx.sky_cover or 0) >= 70 or wx.precip_prob >= 50
            if t_alt is not None and t_alt <= 2 and cloudy:
                findings.append(Finding(NOGO if m.ifr else CAUTION, li, where,
                                        f"Possible icing in cloud at {pt_alt:,.0f} ft {where}: estimated "
                                        f"{t_alt:.0f}°C there with mostly cloudy skies (the NWS forecast has no "
                                        "humidity aloft, so cloud at your altitude is assumed)", "ice"))
        elif t_alt is not None and rh_alt is not None and t_alt <= 2 and rh_alt >= 85:
            findings.append(Finding(NOGO if m.ifr else CAUTION, li, where,
                                    f"Clouds near freezing at {pt_alt:,.0f} ft {where} (icing risk in cloud)", "ice"))
        seg_terr = leg.max_terrain(d - WX_STEP_NM / 2, d + WX_STEP_NM / 2)
        if seg_terr > 3000:
            wdir5, wspd5, _, _ = wx.wind_at(max(5000, seg_terr + 1000))
            if wspd5 >= m.mountain_wind_caution_kt:
                findings.append(Finding(CAUTION, li, where, f"Strong winds over ridges ({wspd5:.0f} kt) {where}: "
                                        "expect turbulence / mountain wave", "turb"))
        for adv in ctx.advisories:
            if adv.hazard in IGNORED_HAZARDS or not adv.covers(la, lo, eta, pt_alt):
                continue
            hz = adv.hazard
            if "CONV" in hz or adv.kind == "CONVECTIVE SIGMET":
                lvl = NOGO
            elif hz in ("IFR", "MTN OBSCN", "MT_OBSC", "MTN_OBSC") and not m.ifr:
                lvl = NOGO if (hz != "IFR" and seg_terr > 3000) else CAUTION
            elif hz.startswith("ICE") or hz == "ICE":
                lvl = NOGO if m.ifr else CAUTION
            else:
                lvl = CAUTION
            findings.append(Finding(lvl, li, where, f"{adv.kind} {hz} in effect {where} at your ETA", "adv"))

    arr_time = sim["arr_time"]
    dep_wx = _airport_wx(ctx, leg.dep, dep_time)
    arr_wx = _airport_wx(ctx, leg.arr, arr_time)
    _check_airport(ctx, dep_wx, li, "departure", findings)
    _check_airport(ctx, arr_wx, li, "arrival", findings)

    # Day / night
    if not m.day_only:
        _night_checks(ctx, leg, li, dep_time, arr_time, sim, findings)
    if m.day_only:
        rise, dep_sset = sun_times(leg.dep.lat, leg.dep.lon, dep_time, ctx.tz)
        arr_rise, sset = sun_times(leg.arr.lat, leg.arr.lon, arr_time, ctx.tz)
        if dep_time < rise:
            findings.append(Finding(NOGO, li, leg.dep.ident, f"Departs {leg.dep.ident} before sunrise "
                                    f"({ctx.fmt(rise)})", "night"))
        elif dep_time > dep_sset:
            findings.append(Finding(NOGO, li, leg.dep.ident, f"Departs {leg.dep.ident} after sunset "
                                    f"({ctx.fmt(dep_sset)})", "night"))
        if arr_time < arr_rise or ctx.local(arr_time).date() != ctx.local(dep_time).date() \
                or arr_time > sset - timedelta(minutes=m.sunset_buffer_minutes):
            findings.append(Finding(NOGO, li, leg.arr.ident, f"Lands {leg.arr.ident} at {ctx.fmt(arr_time)}, "
                                    f"sunset {ctx.fmt(sset)} (day-only, {m.sunset_buffer_minutes:.0f} min buffer)",
                                    "night"))

    # Weight & balance at takeoff and at landing (after this leg's burn)
    if ctx.wb is not None and ctx.loading is not None:
        from dataclasses import replace as _replace
        wbr = ctx.wb.evaluate(_replace(ctx.loading, fuel_gal=fuel_on_board), burn_gal=sim["fuel"])
        for level, text in wbr.problems:
            if level == NOGO:
                findings.append(Finding(NOGO, li, leg.dep.ident, f"Weight & balance: {text}", "wb"))

    # Fuel and fatigue (91.151: 45 min minimum when the leg ends at night)
    reserve_min = m.reserve_minutes
    if not m.day_only and _is_night(ctx, leg.arr, arr_time):
        reserve_min = max(reserve_min, 45)
    reserve = reserve_min / 60 * ac.cruise_gph
    landing = fuel_on_board - sim["fuel"]
    if landing < reserve:
        findings.append(Finding(NOGO, li, leg.arr.ident, f"Lands {leg.arr.ident} with {landing:.1f} gal; your "
                                f"{reserve_min:.0f}-min reserve needs {reserve:.1f} gal"
                                + (" (night minimum)" if reserve_min > m.reserve_minutes else ""), "fuel"))
    elif landing < reserve + 4:
        findings.append(Finding(CAUTION, li, leg.arr.ident, f"Thin fuel margin into {leg.arr.ident}: "
                                f"{landing:.1f} gal on landing", "fuel"))
    if sim["minutes"] / 60 > m.max_leg_hours:
        findings.append(Finding(CAUTION, li, f"leg {li + 1}", f"Leg {leg.dep.ident}-{leg.arr.ident} is "
                                f"{sim['minutes'] / 60:.1f} h (your limit {m.max_leg_hours:.1f} h)", "fatigue"))

    profile = []
    for (la, lo, d), eta in zip(leg.samples, sim["etas"]):
        wx = ctx.forecast.at(la, lo, eta)
        cig = wx.ceiling_agl_ft() if wx else None
        a = alt if cs - 1 <= d <= ce + 1 else (leg.dep.elev_ft if d < cs else leg.arr.elev_ft)
        profile.append(ProfilePoint(d, a, leg.max_terrain(d - WX_STEP_NM / 2, d + WX_STEP_NM / 2),
                                    (wx.surface_elev_ft + cig) if (wx and cig is not None) else None, eta))

    mag_hdg = (leg.mag_course + sim["wca"]) % 360
    return LegResult(leg, li, dep_time, arr_time, alt, sim["minutes"], sim["fuel"], landing, reserve,
                     sim["avg_gs"], sim["avg_comp"], mag_hdg, profile, dep_wx, arr_wx, _dedupe(findings))


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse repeats of the same hazard kind on a leg into one line."""
    out: dict[tuple, Finding] = {}
    counts: dict[tuple, int] = {}
    for f in findings:
        k = (f.leg, f.text if f.kind == "wb" else (f.kind or f.text), f.level)
        if k not in out:
            out[k] = f
            counts[k] = 1
        else:
            counts[k] += 1
    res = []
    for k, f in out.items():
        if counts[k] > 1 and f.kind not in ("fuel", "night", "fatigue", "cig", "vis", "wind", "xwind"):
            f = Finding(f.level, f.leg, f.where, f"{f.text} (+{counts[k] - 1} more spots on this leg)", f.kind)
        res.append(f)
    return res


def confidence(ctx: Context, dep_time: datetime) -> str:
    h = (dep_time - ctx.now).total_seconds() / 3600
    if h <= 30:
        return "High (TAF range)"
    if h <= 72:
        return "Moderate (1-3 days)"
    if h <= 168:
        return "Low (4-7 days)"
    return "Outlook only (8+ days)"


def evaluate_plan(ctx: Context, plan: RoutePlan, dep_time: datetime) -> PlanResult:
    t = dep_time
    legs: list[LegResult] = []
    findings: list[Finding] = []
    for i, leg in enumerate(plan.legs):
        lr = evaluate_leg(ctx, leg, i, t, ctx.ac.usable_gal)
        legs.append(lr)
        findings += lr.findings
        t = lr.arr_time + timedelta(minutes=ctx.mins.fuel_stop_ground_minutes)
    if dep_time < ctx.now:
        findings.append(Finding(NOGO, 0, "", "Departure time is in the past", "past"))
    return PlanResult(plan, dep_time, legs[-1].arr_time, legs, findings, confidence(ctx, dep_time))


@dataclass
class DayResult:
    day: date
    best: PlanResult | None
    options: list[PlanResult]   # best result per route option, ranked
    by_hour: list[PlanResult]   # best route at each departure hour

    @property
    def status(self) -> str:
        return self.best.status if self.best else "NO-GO"


def search_days(ctx: Context, plans: list[RoutePlan], start: date, end: date,
                earliest_hour: int = 7, latest_hour: int = 14, step_minutes: int = 60) -> list[DayResult]:
    tz = ZoneInfo(ctx.tz)
    horizon = ctx.forecast.horizon
    results: list[DayResult] = []
    day = start
    while day <= end:
        per_plan: dict[str, PlanResult] = {}
        by_hour: list[PlanResult] = []
        minute = earliest_hour * 60
        while minute <= latest_hour * 60:
            dep = datetime.combine(day, time(minute // 60, minute % 60), tz).astimezone(timezone.utc)
            minute += step_minutes
            if dep < ctx.now or (horizon and dep > horizon):
                continue
            hour_best = None
            for p in plans:
                r = evaluate_plan(ctx, p, dep)
                cur = per_plan.get(p.route_string)
                if cur is None or r.score < cur.score:
                    per_plan[p.route_string] = r
                if hour_best is None or r.score < hour_best.score:
                    hour_best = r
            if hour_best:
                by_hour.append(hour_best)
        options = sorted(per_plan.values(), key=lambda r: r.score)
        results.append(DayResult(day, options[0] if options else None, options, by_hour))
        day += timedelta(days=1)
    return results


def route_weather_rows(ctx: Context, r: PlanResult) -> list[dict]:
    """What the forecast says at each 20-nm point along the plan, at your ETA there."""
    rows = []
    for lr in r.legs:
        leg = lr.leg
        cs = next((p.dist_nm for p in lr.profile if p.alt_ft == lr.alt_ft), 0)
        for p, (la, lo, d) in zip(lr.profile, leg.samples):
            wx = ctx.forecast.at(la, lo, p.eta)
            if wx is None:
                rows.append({"Leg": f"{leg.dep.ident}-{leg.arr.ident}", "ETA": ctx.fmt(p.eta),
                             "Where": _near(ctx, la, lo), "Conditions": "beyond forecast range"})
                continue
            wdir, wspd, t_alt, _ = wx.wind_at(p.alt_ft if p.alt_ft > 2000 else lr.alt_ft)
            gs, _ = wind_triangle(leg.true_course, 100, wdir, wspd)
            comp = gs - 100
            cig = wx.ceiling_agl_ft()
            rows.append({
                "Leg": f"{leg.dep.ident}-{leg.arr.ident}",
                "ETA": ctx.fmt(p.eta),
                "Where": _near(ctx, la, lo).replace("near ", ""),
                "Conditions": wx.wx_text,
                "Cloud base (MSL)": "none" if cig is None else f"{wx.surface_elev_ft + cig:,.0f}",
                "Terrain": f"{p.terrain_ft:,.0f}",
                "Vis (sm)": "—" if wx.vis_sm is None else ("10+" if wx.vis_sm >= 10 else f"{wx.vis_sm:.0f}"),
                "Rain %": f"{wx.precip_prob:.0f}",
                "Storm risk": {"likely": "HIGH", "some": "some", "low": "low"}[
                    wx.storm_level(ctx.mins.cape_caution_jkg, ctx.mins.cape_nogo_jkg)],
                f"Wind @ {lr.alt_ft:,.0f}": f"{wdir:03.0f}@{wspd:.0f} ({'tail' if comp >= 0 else 'head'} {abs(comp):.0f})",
                "Temp @ alt": "—" if t_alt is None else f"{t_alt:.0f}°C",
            })
    return rows



# ---------------------------------------------------------------------------
# Night flying (used when the pilot allows night flights)
# ---------------------------------------------------------------------------
def _is_night(ctx: "Context", ap, when: datetime) -> bool:
    """Between sunset and sunrise at `ap` (14 CFR 91.209 position-light period)."""
    rise, sset = sun_times(ap.lat, ap.lon, when, ctx.tz)
    return when < rise or when > sset


def _in_currency_window(ctx: "Context", ap, when: datetime) -> bool:
    """61.57(b): from 1 hour after sunset to 1 hour before sunrise."""
    rise, sset = sun_times(ap.lat, ap.lon, when, ctx.tz)
    return when < rise - timedelta(hours=1) or when > sset + timedelta(hours=1)


def _night_checks(ctx: "Context", leg: "Leg", li: int, dep_time: datetime, arr_time: datetime,
                  sim: dict, findings: list) -> None:
    m = ctx.mins
    night_dep, night_arr = _is_night(ctx, leg.dep, dep_time), _is_night(ctx, leg.arr, arr_time)
    if night_arr or night_dep:
        _, sset = sun_times(leg.arr.lat, leg.arr.lon, arr_time, ctx.tz)
        findings.append(Finding(INFO, li, leg.arr.ident, f"Night leg: lands {leg.arr.ident} at {ctx.fmt(arr_time)} "
                                f"(sunset {ctx.fmt(sset)})", "night-info"))
    if not m.night_current and (_in_currency_window(ctx, leg.dep, dep_time)
                                or _in_currency_window(ctx, leg.arr, arr_time)):
        findings.append(Finding(CAUTION, li, leg.arr.ident,
                                f"Night takeoff/landing more than 1 h after sunset at {leg.arr.ident if night_arr else leg.dep.ident}: "
                                "you need 3 full-stop night landings in the last 90 days to carry passengers "
                                "(14 CFR 61.57(b)). Tick 'Night current' if you have them.", "night-currency"))
    # Night over mountains: few lit places to put it down if the engine quits.
    if leg.mountainous:
        cs, ce = sim["cruise"]
        dark = [d for (la, lo, d), eta in zip(leg.samples, sim["etas"])
                if leg.max_terrain(d - WX_STEP_NM / 2, d + WX_STEP_NM / 2) > 3000
                and _is_night(ctx, leg.dep if d < leg.dist_nm / 2 else leg.arr, eta)]
        if dark:
            findings.append(Finding(CAUTION, li, f"leg {leg.dep.ident}-{leg.arr.ident}",
                                    f"Night over mountainous terrain (up to ~{leg.max_terrain():,.0f} ft) on "
                                    f"{leg.dep.ident}-{leg.arr.ident}: few lit landing options if the engine "
                                    "quits. Consider a higher altitude, a route around the ridges, or crossing "
                                    "them in daylight.", "night-terrain"))
