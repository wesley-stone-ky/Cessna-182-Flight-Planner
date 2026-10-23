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


def test_lex_to_st_pete_needs_a_stop(db, ac, mins):
    # 625 nm: too far for 56 gal standard tanks, and too long for the 3.5 h leg limit with 75 gal
    small = Aircraft.load(overrides={"fuel.usable_gal": 56})
    assert all(p.stops for p in candidate_plans(db, db.get("KLEX"), db.get("KSPG"), small, mins))
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
    assert plans, "should find at least one fuel-stop route"
    assert all(p.stops for p in plans), "625 nm is over the 3.5 h leg limit"


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


# --- live products -------------------------------------------------------------
REAL_METAR = {"icaoId": "KLEX", "obsTime": 1792720440, "reportTime": "2026-10-23T02:00:00.000Z", "temp": 11.1,
              "dewp": 4.4, "wdir": 50, "wspd": 4, "visib": "10+", "altim": 1024.1,
              "rawOb": "METAR KLEX 230154Z 05004KT 10SM CLR 11/04 A3024 RMK AO2", "lat": 38.0339,
              "lon": -84.6114, "cover": "CLR", "clouds": [], "fltCat": "VFR"}


def test_decode_real_metar_shape():
    from flightplanner.weather import decode_metar
    d = decode_metar(REAL_METAR)
    assert d["category"] == "VFR" and d["vis_sm"] == 10 and d["ceiling_ft"] is None
    assert d["wind_text"].startswith("050@4") and d["altim_inhg"] == pytest.approx(30.24, abs=0.01)
    bkn = dict(REAL_METAR, wdir="VRB", clouds=[{"cover": "BKN", "base": 900}], fltCat=None, visib="2 1/2")
    d2 = decode_metar(bkn)
    assert d2["category"] == "IFR" and d2["ceiling_ft"] == 900 and d2["wind_text"].startswith("VRB")


def test_flight_categories():
    from flightplanner.weather import flight_category
    assert flight_category(None, 10) == "VFR"
    assert flight_category(2500, 10) == "MVFR"
    assert flight_category(800, 10) == "IFR"
    assert flight_category(4000, 0.5) == "LIFR"


def test_gairmet_string_coords_and_borrowed_taf(tmp_path, monkeypatch):
    import flightplanner.weather as w
    monkeypatch.setattr(w, "CACHE_DIR", tmp_path)
    t0 = int(datetime(2026, 10, 23, 12, tzinfo=timezone.utc).timestamp())
    routes = {
        "/gairmet": [{"hazard": "IFR", "validTime": "2026-10-23T12:00:00.000Z", "base": None, "top": None,
                      "due_to": "CIG BLW 010/VIS BLW 3SM BR",
                      "coords": [{"lat": "33.0", "lon": "-80.0"}, {"lat": "34.0", "lon": "-80.0"},
                                 {"lat": "34.0", "lon": "-79.0"}, {"lat": "33.0", "lon": "-79.0"}]},
                     {"hazard": "FZLVL", "validTime": "2026-10-23T12:00:00.000Z",
                      "coords": [{"lat": "33", "lon": "-80"}, {"lat": "34", "lon": "-80"}, {"lat": "34", "lon": "-79"}]}],
        "/taf": [{"icaoId": "KMYR", "rawTAF": "TAF KMYR ...", "fcsts": [
            {"timeFrom": t0, "timeTo": t0 + 86400, "wdir": 60, "wspd": 8, "visib": "6+",
             "clouds": [{"cover": "SKC", "base": None}]}]}],
        "/metar": [dict(REAL_METAR, icaoId="KMYR")],
    }
    awc = AviationWeather(session=_FakeSession(routes))
    adv = awc.advisories()
    ifr = [a for a in adv if a.hazard == "IFR"][0]
    assert ifr.kind == "G-AIRMET" and ifr.covers(33.5, -79.5, datetime(2026, 10, 23, 12, 30, tzinfo=timezone.utc))
    db = AirportDB.load()
    metars, tafs = awc.for_airports([db.get("KGGE")], db.nearby)
    assert tafs["KGGE"]["station"] == "KMYR" and tafs["KGGE"]["dist_nm"] > 10
    assert metars["KGGE"]["_station"] == "KMYR"
    assert awc.status["TAFs"].ok and awc.status["G-AIRMETs"].count == 2


def test_no_departing_after_dark(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 22, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    r = evaluate_plan(ctx, plans[0], dep)
    assert r.status == "NO-GO" and any("after sunset" in f.text for f in r.findings)


def test_outlook_and_route_weather(db, ac, mins):
    from flightplanner.outlook import airport_outlook, hourly_rows, route_outlook
    from flightplanner.planner import route_weather_rows
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    rows = airport_outlook(ctx.forecast, db.get("KLEX"), mins, ctx.tz, days=7)
    assert len(rows) == 7 and {"Good", "Poor"} <= {r.verdict for r in rows}
    assert hourly_rows(ctx.forecast, db.get("KLEX"), rows[1].day, ctx.tz)
    rt = route_outlook(ctx.forecast, plans[0].legs, mins, ctx.tz, days=7)
    assert any(r["verdict"] == "Poor" for r in rt)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 9, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    wx_rows = route_weather_rows(ctx, evaluate_plan(ctx, plans[0], dep))
    assert len(wx_rows) == len(plans[0].legs[0].samples) and "Cloud base (MSL)" in wx_rows[1]


def test_live_map_escapes_text():
    from flightplanner.livemap import live_map_html
    from flightplanner.weather import Pirep
    html = live_map_html([[(38.0, -84.6), (33.3, -79.3)]],
                         [{"ident": "KLEX", "lat": 38.0, "lon": -84.6, "cat": "VFR", "raw": "<script>x</script>"}],
                         [], [Pirep(None, 35.0, -82.0, 7000, "C172", "MOD CHOP", "", "", "</script><b>")],
                         datetime.now(timezone.utc))
    assert "<script>x</script>" not in html and "</script><b>" not in html
    assert "nexrad" in html and "leaflet" in html


def test_forecast_request_is_light_and_handles_rate_limit(tmp_path, monkeypatch):
    import flightplanner.weather as w
    monkeypatch.setattr(w, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    calls = []

    class R:
        status_code = 429
        headers = {"Retry-After": "1"}
        content = b"x"

    class S:
        def get(self, url, params=None, timeout=None, headers=None):
            calls.append(params)
            return R()

    db = AirportDB.load()
    ac, mins = Aircraft.load(), load_minimums()
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)[:4]
    pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
    with pytest.raises(w.RateLimited):
        w.fetch_model_forecast(pts, session=S())
    p0 = calls[0]
    n_vars = len(p0["hourly"].split(","))
    n_pts = len(p0["latitude"].split(","))
    assert n_vars <= 30, n_vars                 # <= 3 "calls" per location on Open-Meteo's meter
    assert n_pts <= 50, n_pts
    assert p0["forecast_days"] == 10
    assert len(calls) == 3                      # retried politely, then gave up
