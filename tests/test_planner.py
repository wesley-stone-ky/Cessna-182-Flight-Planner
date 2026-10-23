"""Offline tests: no network needed (synthetic weather, fallback terrain)."""
from __future__ import annotations

import xml.dom.minidom
from datetime import date, datetime, timedelta, timezone

import pytest

from flightplanner.aircraft import Aircraft
from flightplanner.airports import AirportDB
from flightplanner.config import load_minimums
from flightplanner.demo import synthetic_forecast
from flightplanner.export import leg_fpl, leg_gfp, navlog_csv, text_brief
from flightplanner.geo import haversine_nm, point_in_polygon, wind_components, wind_triangle
from flightplanner.planner import (Context, attach_terrain, candidate_plans, evaluate_plan,
                                   forecast_points, hemispheric_altitudes, search_days)
from flightplanner.terrain import Terrain
from flightplanner.weather import AviationWeather, taf_at


class _NoNet:
    """Stand-in requests session that always fails, forcing offline fallbacks."""
    headers: dict = {}

    def get(self, *a, **k):
        raise ConnectionError("offline test")


@pytest.fixture(scope="module")
def db():
    return AirportDB.load()


@pytest.fixture(scope="module")
def ac():
    return Aircraft.load()


@pytest.fixture(scope="module")
def mins():
    return load_minimums()


def _ctx(db, ac, mins, plans, now=None):
    now = now or datetime.now(timezone.utc)
    attach_terrain(plans, Terrain(session=_NoNet()), mins)
    pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
    fc = synthetic_forecast(pts, start=now - timedelta(hours=1), days=8)
    return Context(ac, mins, fc, db, now=now)


# --- geometry -----------------------------------------------------------------
def test_distances_match_charts(db):
    lex, gge, spg = db.get("KLEX"), db.get("GGE"), db.get("KSPG")
    assert 375 < haversine_nm(lex.lat, lex.lon, gge.lat, gge.lon) < 395
    assert 615 < haversine_nm(lex.lat, lex.lon, spg.lat, spg.lon) < 635


def test_wind_triangle_headwind_and_tailwind():
    gs, wca = wind_triangle(180, 130, 180, 20)   # straight headwind
    assert gs == pytest.approx(110, abs=0.5) and wca == pytest.approx(0, abs=0.1)
    gs, _ = wind_triangle(180, 130, 0, 20)       # straight tailwind
    assert gs == pytest.approx(150, abs=0.5)
    gs, wca = wind_triangle(180, 130, 270, 20)   # wind from the right -> correct right
    assert wca > 0


def test_crosswind():
    head, cross = wind_components(220, 270, 20)
    assert cross == pytest.approx(15.3, abs=0.2) and head > 0


def test_hemispheric_rule():
    assert hemispheric_altitudes(150, False, 10500) == [3500, 5500, 7500, 9500]
    assert hemispheric_altitudes(300, False, 10500) == [4500, 6500, 8500, 10500]
    assert hemispheric_altitudes(10, True, 12000) == [3000, 5000, 7000, 9000, 11000]


def test_point_in_polygon():
    sq = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert point_in_polygon(5, 5, sq) and not point_in_polygon(15, 5, sq)


# --- aircraft -----------------------------------------------------------------
def test_climb_from_field_elevation(ac):
    from_sl = ac.climb(0, 8000)
    from_lex = ac.climb(979, 8000)
    assert from_lex.minutes < from_sl.minutes and from_lex.gal < from_sl.gal


def test_standard_tanks_cannot_go_lex_to_st_pete_nonstop(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
    assert plans, "should find at least one fuel-stop route"
    assert all(p.stops for p in plans), "56 gal usable + 1 h reserve can't do 625 nm"


def test_long_range_tanks_allow_more_options(db, mins):
    big = Aircraft.load(overrides={"fuel.usable_gal": 88})
    relaxed = load_minimums({"max_leg_hours": 5.5})
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), big, relaxed)
    assert any(not p.stops for p in plans)


def test_georgetown_nonstop_offered(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    assert plans[0].label == "Nonstop"


# --- evaluation ---------------------------------------------------------------
def test_good_day_vs_frontal_day(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    today = ctx.local(ctx.now).date()
    days = search_days(ctx, plans, today + timedelta(days=1), today + timedelta(days=5), 8, 13)
    statuses = {d.day: d.status for d in days}
    assert "GO" in statuses.values()
    assert "NO-GO" in statuses.values(), "the synthetic cold front should ground at least one day"


def test_late_departure_hits_sunset(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    r = evaluate_plan(ctx, plans[0], dep)
    assert r.status == "NO-GO"
    assert any(f.kind == "night" for f in r.findings)


def test_fuel_accounting_is_sane(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 9, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    r = evaluate_plan(ctx, plans[0], dep)
    leg = r.legs[0]
    assert 2.0 < leg.minutes / 60 < 4.0
    assert 25 < leg.fuel_used_gal < 50
    assert leg.fuel_at_landing_gal == pytest.approx(ac.usable_gal - leg.fuel_used_gal)


# --- official products parsing -------------------------------------------------
class _FakeResp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200
        self.content = b"x"

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        for key, data in self.routes.items():
            if url.endswith(key):
                return _FakeResp(data)
        return _FakeResp([])


def test_taf_and_sigmet_parsing(tmp_path, monkeypatch):
    import flightplanner.weather as w
    monkeypatch.setattr(w, "CACHE_DIR", tmp_path)
    t0 = int(datetime(2026, 10, 23, 12, tzinfo=timezone.utc).timestamp())
    routes = {
        "/taf": [{"icaoId": "KGGE", "rawTAF": "TAF KGGE ...", "fcsts": [
            {"timeFrom": t0, "timeTo": t0 + 6 * 3600, "wdir": 230, "wspd": 8, "visib": "6+",
             "clouds": [{"cover": "SCT", "base": 4000}]},
            {"timeFrom": t0 + 2 * 3600, "timeTo": t0 + 4 * 3600, "fcstChange": "TEMPO", "visib": "3",
             "clouds": [{"cover": "BKN", "base": 1500}], "wxString": "-TSRA"}]}],
        "/airsigmet": [{"airSigmetType": "SIGMET", "hazard": "CONVECTIVE", "validTimeFrom": t0,
                        "validTimeTo": t0 + 7200, "coords": [{"lat": 33, "lon": -80}, {"lat": 34, "lon": -80},
                                                            {"lat": 34, "lon": -79}, {"lat": 33, "lon": -79}]}],
    }
    awc = AviationWeather(session=_FakeSession(routes))
    tafs = awc.tafs(["KGGE"])
    prev, temps = taf_at(tafs["KGGE"], datetime.fromtimestamp(t0 + 3 * 3600, tz=timezone.utc))
    assert prev.vis_sm == 6 and prev.ceiling_ft is None
    assert temps and temps[0].ceiling_ft == 1500
    adv = awc.advisories()
    assert adv and adv[0].kind == "CONVECTIVE SIGMET"
    assert adv[0].covers(33.3, -79.3, datetime.fromtimestamp(t0 + 600, tz=timezone.utc))


# --- exports ------------------------------------------------------------------
def test_exports(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 9, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    r = evaluate_plan(ctx, plans[0], dep)
    for lr in r.legs:
        xml.dom.minidom.parseString(leg_fpl(lr.leg))
        assert leg_gfp(lr.leg).startswith("FPN/RI:F:")
    assert navlog_csv(ctx, r).count("\n") == len(r.legs) + 1
    assert "SCREENING ONLY" in text_brief(ctx, r)
