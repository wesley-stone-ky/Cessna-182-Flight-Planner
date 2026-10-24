"""Offline tests: no network needed (synthetic weather, fallback terrain)."""
from __future__ import annotations

import xml.dom.minidom
from pathlib import Path
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
    mins = load_minimums({"day_only": True})
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
    mins = load_minimums({"day_only": True})
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


def _nws_props(t0_iso: str):
    """A realistic api.weather.gov gridpoint 'properties' block (trimmed)."""
    def layer(uom, vals):
        return {"uom": uom, "values": [{"validTime": v, "value": x} for v, x in vals]}
    return {
        "updateTime": t0_iso,
        "elevation": {"unitCode": "wmoUnit:m", "value": 300.0},
        "temperature": layer("wmoUnit:degC", [(f"{t0_iso}/PT6H", 15.0), (f"{t0_iso[:11]}18:00:00+00:00/P1DT6H", 18.0)]),
        "dewpoint": layer("wmoUnit:degC", [(f"{t0_iso}/P2D", 10.0)]),
        "skyCover": layer("wmoUnit:percent", [(f"{t0_iso}/PT3H", 90), (f"{t0_iso[:11]}15:00:00+00:00/P2D", 20)]),
        "ceilingHeight": layer("wmoUnit:m", [(f"{t0_iso}/PT3H", 300), (f"{t0_iso[:11]}15:00:00+00:00/P2D", None)]),
        "visibility": layer("wmoUnit:m", [(f"{t0_iso}/PT3H", 3218.7), (f"{t0_iso[:11]}15:00:00+00:00/P2D", 16093)]),
        "windSpeed": layer("wmoUnit:km_h-1", [(f"{t0_iso}/P2D", 18.52)]),
        "windDirection": layer("wmoUnit:degree_(angle)", [(f"{t0_iso}/P2D", 230)]),
        "windGust": layer("wmoUnit:km_h-1", [(f"{t0_iso}/P2D", 37.04)]),
        "transportWindSpeed": layer("wmoUnit:km_h-1", [(f"{t0_iso}/P2D", 46.3)]),
        "transportWindDirection": layer("wmoUnit:degree_(angle)", [(f"{t0_iso}/P2D", 250)]),
        "probabilityOfPrecipitation": layer("wmoUnit:percent", [(f"{t0_iso}/P2D", 60)]),
        "probabilityOfThunder": layer("wmoUnit:percent", [(f"{t0_iso}/P2D", 45)]),
        "weather": {"values": [{"validTime": f"{t0_iso}/P2D", "value": [
            {"coverage": "chance", "weather": "thunderstorms", "intensity": None, "attributes": []},
            {"coverage": "likely", "weather": "rain_showers", "intensity": "light", "attributes": []}]}]},
    }


def test_nws_grid_parsing(tmp_path, monkeypatch):
    import flightplanner.nws as nws
    import flightplanner.weather as w
    monkeypatch.setattr(w, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(nws, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(nws, "_POINTS_FILE", tmp_path / "pts.json")
    monkeypatch.setattr(nws.time, "sleep", lambda s: None)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t0 = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00+00:00")
    # make the second interval start 3 h after t0
    props = _nws_props(t0)
    for lay in ("skyCover", "ceilingHeight", "visibility"):
        props[lay]["values"][1]["validTime"] = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00+00:00") + "/P2D"

    class Resp:
        def __init__(self, code, data=None):
            self.status_code, self._d, self.headers, self.content = code, data, {}, b"x"
        def json(self):
            return self._d
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    class Sess:
        def get(self, url, params=None, timeout=None, headers=None):
            if "/points/27.5" in url:            # a point over the Gulf: NWS has no grid there
                return Resp(404)
            if "/points/" in url:
                return Resp(200, {"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/LMK/80,61"}})
            return Resp(200, {"properties": props})

    fc, status = w.fetch_forecast([(38.0, -84.5), (27.5, -82.75)], session=Sess())
    assert status["NWS forecast (weather.gov)"].ok and status["NWS forecast (weather.gov)"].count == 1
    assert list(status) == ["NWS forecast (weather.gov)"]
    wx = fc.at(38.0, -84.5, now)
    assert wx.nws and wx.ceiling_agl_ft() == pytest.approx(984, abs=2)       # 300 m ceiling
    assert wx.vis_sm == pytest.approx(2.0, abs=0.05)
    assert wx.wind_kt == pytest.approx(10, abs=0.1) and wx.gust_kt == pytest.approx(20, abs=0.1)
    assert wx.storm_level(500, 1500) == "likely" and wx.wcode == 95
    later = fc.at(38.0, -84.5, now + timedelta(hours=6))
    assert later.ceiling_agl_ft() is None and later.vis_sm > 9                # clears up
    wdir, wspd, _, _ = later.wind_at(6000)
    assert wspd == pytest.approx(25, abs=1) and 240 <= wdir <= 260           # transport wind used aloft
    assert later.aloft_estimated
    assert later.freezing_level_ft == pytest.approx(984 + 18 * 500, abs=5)   # 18 C at 2 C/1,000 ft above 300 m
    # the Gulf point borrows the nearest grid instead of failing
    assert fc.at(27.5, -82.75, now) is not None


def test_outlook_explains_itself_and_does_not_fail_partial_days(db, ac, mins):
    from flightplanner.outlook import airport_outlook, route_outlook
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)[:2]
    now = datetime.now(timezone.utc)
    attach_terrain(plans, Terrain(session=_NoNet()), mins)
    pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
    # forecast that stops at ~10 AM local on day 5, like the end of the NWS grid
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("America/New_York")
    end_local = datetime.combine(now.astimezone(zone).date() + timedelta(days=5),
                                 datetime.min.time(), zone) + timedelta(hours=10)
    hours = int((end_local.astimezone(timezone.utc) - (now - timedelta(hours=1))).total_seconds() // 3600)
    fc = synthetic_forecast(pts, start=now - timedelta(hours=1), days=8)
    for k, srs in fc.series.items():
        for name, arr in srs["hourly"].items():
            srs["hourly"][name] = arr[:hours]
    from flightplanner.weather import GridForecast
    fc = GridForecast(fc.series)
    rows = airport_outlook(fc, db.get("KLEX"), mins, days=7)
    by_day = {r.day: r for r in rows}
    partial = by_day[end_local.date()]
    assert partial.verdict == "Partial", partial.verdict
    assert any("only covers" in line for line in partial.reasons)
    frontal = [r for r in rows if r.verdict == "Poor"]
    assert frontal and any("Low ceilings" in l or "Gusty" in l for r in frontal for l in r.reasons)
    assert all(r.reasons for r in rows)
    rt = route_outlook(fc, plans[0].legs, mins, days=7)
    bad = [r for r in rt if r["verdict"] == "Poor"]
    assert bad and any("near K" in line for line in bad[0]["reasons"])


# --- airport surface wind -----------------------------------------------------
def _taf_ctx(db, ac, mins, ident, periods, now):
    ap = db.get(ident)
    fc = synthetic_forecast([(ap.lat, ap.lon)], start=now - timedelta(hours=3), days=1)
    return Context(ac, mins, fc, db, tafs={ident: {"station": ident, "periods": periods}}, now=now)


def _per(now, h0, h1, wdir, kt, gust=None, change=""):
    from flightplanner.weather import TafPeriod
    return TafPeriod(now + timedelta(hours=h0), now + timedelta(hours=h1), change, wdir, kt, gust, 10.0, None, "")


def _airport_findings(ctx, ident, when):
    from flightplanner.planner import _airport_wx, _check_airport
    aw = _airport_wx(ctx, ctx.db.get(ident), when)
    out = []
    _check_airport(ctx, aw, 0, "arrival", out)
    return aw, out


def test_variable_wind_counts_as_full_crosswind(db, ac, mins):
    now = datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    ctx = _taf_ctx(db, ac, mins, "KLEX", [_per(now, -3, 3, None, 14)], now)
    aw, out = _airport_findings(ctx, "KLEX", now)
    assert aw.crosswind_kt == 14
    assert any(f.kind == "xwind" and f.level == "CAUTION" for f in out)


def test_crosswind_uses_gust(db, ac, mins):
    # 180@8G20 at KLEX: rwy 22 sustained crosswind ~5 kt, but ~13 kt with the gust
    now = datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    ctx = _taf_ctx(db, ac, mins, "KLEX", [_per(now, -3, 3, 180, 8, 20)], now)
    aw, out = _airport_findings(ctx, "KLEX", now)
    assert aw.best_runway == "22" and 12 < aw.crosswind_kt < 15
    assert any(f.kind == "xwind" for f in out)


def test_wind_shift_after_eta_is_caught(db, ac, mins):
    now = datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    periods = [_per(now, -3, 0.75, 40, 5), _per(now, 0.75, 4, 40, 25, 35, "FM")]
    ctx = _taf_ctx(db, ac, mins, "KLEX", periods, now)
    aw, out = _airport_findings(ctx, "KLEX", now)
    assert aw.gust_kt == 35
    assert any(f.kind == "wind" and f.level == "NO-GO" for f in out)


def test_tempo_wind_and_gust_spread(db, ac, mins):
    now = datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    periods = [_per(now, -3, 3, 40, 5), _per(now, -1, 2, 40, 8, 23, "TEMPO")]
    ctx = _taf_ctx(db, ac, mins, "KLEX", periods, now)
    aw, out = _airport_findings(ctx, "KLEX", now)
    assert aw.gust_spread_kt == 15
    assert any("above the sustained" in f.text for f in out)


def test_missing_runway_data_is_flagged(db, ac, mins):
    import dataclasses
    from flightplanner.planner import _airport_wx, _check_airport
    now = datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    ctx = _taf_ctx(db, ac, mins, "KLEX", [_per(now, -3, 3, 270, 10)], now)
    ap = dataclasses.replace(db.get("KLEX"), runways=())
    aw = _airport_wx(ctx, ap, now)
    out = []
    _check_airport(ctx, aw, 0, "arrival", out)
    assert aw.crosswind_kt is None
    assert any("crosswind not checked" in f.text for f in out)


# --- weight & balance ---------------------------------------------------------
def test_wb_normal_load_is_in_limits():
    from flightplanner.wb import Loading, WeightBalance
    wb = WeightBalance()
    r = wb.evaluate(Loading(front_lb=340, bag_a_lb=40), burn_gal=35)
    assert r.ok, r.problems
    to, land = r.points[0], r.points[1]
    assert to.weight_lb <= 3100 and land.weight_lb < to.weight_lb
    assert 33.0 <= to.cg_in <= 46.0


def test_wb_overweight_limits_fuel_and_flags_aft_cg():
    from flightplanner.wb import Loading, WeightBalance
    wb = WeightBalance()
    heavy = Loading(front_lb=400, rear_lb=350, bag_a_lb=120, bag_b_lb=80)
    assert wb.max_fuel(heavy) < 75                         # can't fill the tanks with this load
    r = wb.evaluate(Loading(400, 350, 120, 80, fuel_gal=75))
    texts = " ".join(t for _, t in r.problems)
    assert not r.ok and "over the 3,100 lb" in texts and "aft of" in texts


def test_wb_baggage_limits_and_envelope_edges():
    from flightplanner.wb import Loading, WeightBalance
    wb = WeightBalance()
    r = wb.evaluate(Loading(340, 0, 150, 0, fuel_gal=40))
    assert any("area A" in t for _, t in r.problems)
    assert wb.fwd_limit(2250) == pytest.approx(33.0) and wb.fwd_limit(3100) == pytest.approx(40.9)
    assert wb.fwd_limit(2675) == pytest.approx((33.0 + 40.9) / 2, abs=0.01)
    assert wb.aft_limit(2800) == pytest.approx(46.0)


def test_wb_problems_become_no_go_findings(db, mins):
    from flightplanner.wb import Loading, WeightBalance
    ac = Aircraft.load()
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    ctx.wb, ctx.loading = WeightBalance(), Loading(400, 350, 120, 80)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, 9, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    r = evaluate_plan(ctx, plans[0], dep)
    assert r.status == "NO-GO" and any(f.kind == "wb" for f in r.findings)


# --- NWS-only forecast: no winds or humidity aloft ------------------------------
def _nws_only(ctx, sky=None):
    """Mark the synthetic forecast the way NWS grids are: winds/temps aloft are estimates."""
    for ser in ctx.forecast.series.values():
        n = len(ser["hourly"]["time"])
        ser["hourly"]["aloft_estimated"] = [1] * n
        if sky is not None:
            ser["hourly"]["sky_cover"] = [sky] * n
    ctx.forecast._cache.clear()
    return ctx


def _morning(ctx, days=1, hour=8):
    from zoneinfo import ZoneInfo
    day = ctx.local(ctx.now).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)


def test_estimated_winds_aloft_get_no_tailwind_credit(db, ac, mins):
    # KLEX -> KGGE flies south-east; the synthetic 250-degree winds aloft are a tailwind there.
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    dep = _morning(ctx)
    with_winds = evaluate_plan(ctx, plans[0], dep).legs[0]
    est = evaluate_plan(_nws_only(ctx), plans[0], dep).legs[0]
    tas = ac.ktas(est.alt_ft)
    assert est.avg_gs_kt <= tas - mins.planning_headwind_kt + 0.5
    assert est.minutes >= with_winds.minutes
    assert any(f.kind == "winds" and f.level == "INFO" for f in est.findings)


def test_estimated_icing_flags_cold_cloudy_altitudes(db, ac, mins):
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    dep = _morning(ctx)
    cloudy = evaluate_plan(_nws_only(ctx, sky=90), plans[0], dep)
    assert any(f.kind == "ice" and "humidity aloft" in f.text for f in cloudy.findings)
    clear = evaluate_plan(_nws_only(ctx, sky=10), plans[0], dep)
    assert not any(f.kind == "ice" for f in clear.findings)


def test_terrain_tiles_decode_and_pad(monkeypatch):
    import io as _io
    from PIL import Image
    from flightplanner import terrain as T
    monkeypatch.setattr(T, "_CACHE_FILE", T.CACHE_DIR / "_test_terrain_cache.json")
    # Terrarium encoding: meters = R*256 + G + B/256 - 32768; 1,000 m everywhere
    v = 1000 + 32768
    img = Image.new("RGB", (256, 256), (v // 256, v % 256, 0))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")

    class Resp:
        content = buf.getvalue()
        def raise_for_status(self):
            pass

    class Sess:
        calls = 0
        def get(self, url, **k):
            Sess.calls += 1
            assert "/terrarium/10/" in url
            return Resp()

    t = T.Terrain(session=Sess())
    t.cache = {}
    (e,) = t.elevations([(38.04, -84.61)])
    assert e == pytest.approx(1000 * T.M_TO_FT + T.PEAK_PAD_FT, abs=0.5)
    assert not t.used_fallback and Sess.calls >= 1
    T._CACHE_FILE.unlink(missing_ok=True)


def test_terrain_falls_back_conservatively_offline():
    from flightplanner import terrain as T
    t = T.Terrain(session=_NoNet())
    t.cache = {}
    (e,) = t.elevations([(35.76, -82.26)])           # Mt. Mitchell area
    assert t.used_fallback and e >= 6684


# --- weekend email --------------------------------------------------------------
def test_weekend_days():
    from flightplanner.weekend import weekend_days
    thu, fri, sat, sun = date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)
    assert weekend_days(thu) == [fri, sat, sun]
    assert weekend_days(fri) == [sat, sun]
    assert weekend_days(sat) == [sun]
    assert weekend_days(sun) == [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]


def test_weekend_email_verdicts_and_disclaimer(db, ac, mins):
    from flightplanner.weekend import DISCLAIMER, GO, MARGINAL, NODATA, NOGO, render_html, render_text, \
        subject, weekend_for
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)[:1]
    ctx = _ctx(db, ac, mins, plans)
    today = ctx.local(ctx.now).date()
    wk = weekend_for(ctx.forecast, db.get("KLEX"), mins, ctx.tz, today)
    assert wk.days and all(d.verdict in (GO, MARGINAL, NOGO, NODATA) for d in wk.days)
    assert all(d.reason for d in wk.days)
    txt, html = render_text([wk], mins), render_html([wk], mins)
    assert DISCLAIMER in txt and "Screening only" in html and "reply STOP" in html
    assert subject([wk]).startswith("Weekend flying at KLEX: ")


def test_weekend_email_says_no_data_past_the_forecast(db, ac, mins):
    from flightplanner.weekend import NODATA, weekend_for
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)[:1]
    ctx = _ctx(db, ac, mins, plans)
    far = ctx.local(ctx.now).date() + timedelta(days=20)
    wk = weekend_for(ctx.forecast, db.get("KLEX"), mins, ctx.tz, far)
    assert all(d.verdict == NODATA for d in wk.days)


# --- aircraft catalog ---------------------------------------------------------
def test_dads_182r_numbers_are_unchanged():
    """Golden values for the home airplane, captured before the aircraft catalog refactor."""
    from flightplanner.wb import Loading, WeightBalance
    expect = {"75%": (13.5, [134.0, 137.0, 141.0, 141.0], 8000.0),
              "65%": (11.8, [126.0, 129.5, 133.0, 135.5], 10500.0),
              "55%": (10.2, [115.0, 118.5, 122.0, 124.5], 10500.0)}
    for s, (gph, ktas, mx) in expect.items():
        a = Aircraft.load(cruise_setting=s)
        assert a.cruise_gph == gph and [a.ktas(x) for x in (2000, 5000, 8000, 11000)] == ktas
        assert a.max_alt_ft == mx
    a = Aircraft.load()
    assert a.cruise_setting == "65%" and a.usable_gal == 75 and a.taxi_gal == 2 and a.demo_xwind == 15
    assert a.climb_speed_kt() == 90 and "182R" in a.name
    c, d = a.climb(979, 6500), a.descent(6500, 26)
    assert (c.minutes, c.gal, c.nm) == pytest.approx((9.7815, 2.8355, 17.8025), abs=1e-3)
    assert (d.minutes, d.gal, d.nm) == pytest.approx((12.948, 1.9422, 29.4567), abs=1e-3)
    assert a.still_air_leg(380, 979, 26, 6500, 10) == pytest.approx((3.1174, 39.0933), abs=1e-3)
    r = WeightBalance().evaluate(Loading(340, 0, 40, 0), burn_gal=35)
    assert r.fuel_gal == 75 and not r.problems
    assert [(p.label, round(p.weight_lb), round(p.cg_in, 2)) for p in r.points] == \
        [("Takeoff", 2680, 39.84), ("Landing", 2470, 39.27), ("Zero fuel", 2230, 38.49)]


def test_every_catalog_type_is_sane():
    from flightplanner.aircraft import catalog
    cat = catalog()
    assert len(cat) >= 12
    for tid, t in cat.items():
        a = Aircraft.from_type(tid)
        r = a.raw
        lo, hi = t.years
        assert 1940 < lo <= hi <= 2030, tid
        assert r.get("engine") and r.get("name") and r.get("faa_models"), tid
        assert 10 <= a.demo_xwind <= 25, tid
        assert 0 < a.usable_gal < r["fuel"]["total_gal"] <= 120, tid
        # power settings: more power = more fuel and more speed, at every shared altitude
        sets = sorted(r["cruise_settings"].items(), key=lambda kv: -float(kv[0].rstrip("%")))
        for (hi_s, hi_v), (lo_s, lo_v) in zip(sets, sets[1:]):
            assert hi_v["gph"] > lo_v["gph"], (tid, hi_s, lo_s)
            for alt in set(hi_v["ktas"]) & set(lo_v["ktas"]):
                assert hi_v["ktas"][alt] > lo_v["ktas"][alt], (tid, alt)
        for s_, v in r["cruise_settings"].items():
            ks = [v["ktas"][k] for k in sorted(v["ktas"])]
            assert ks == sorted(ks) and 85 <= ks[0] <= 160, (tid, s_)
            assert 5 <= v["gph"] <= 20, (tid, s_)
        # still-air range at 65% with a 45-min reserve: something a real airplane could do
        a65 = Aircraft.from_type(tid, "65%")
        nm = (a65.usable_gal - a65.taxi_gal - 0.75 * a65.cruise_gph) / a65.cruise_gph * a65.ktas(6000)
        assert 200 < nm < 1100, (tid, round(nm))
        rows = r["climb_table"]
        for k in ("minutes", "gal", "nm"):
            vals = [row[k] for row in sorted(rows, key=lambda x: x["alt_ft"])]
            assert vals == sorted(vals) and vals[0] == 0, (tid, k)
        assert a.climb_speed_kt() < a.ktas(4000) and r["descent"]["gph"] < a.cruise_gph, tid
        assert not t.verified, f"{tid}: only mark verified after checking a real POH"


def test_owner_numbers_win_and_smallest_tank_is_default():
    a = Aircraft.from_type("cessna-172/172P")
    assert a.usable_gal == 40                       # standard tanks until the owner says otherwise
    b = Aircraft.from_type("cessna-172/172P", fuel={"usable_gal": 54, "total_gal": 62})
    assert b.usable_gal == 54
    c = Aircraft.from_type("cessna-172/172P", fuel_option="long_range")
    assert c.usable_gal == 54
    d = Aircraft.from_type("cessna-172/172P", cruise_settings={"65%": {"gph": 8.0}})
    assert d.cruise_gph == 8.0 and d.ktas(6000) == 111   # only the gph was tuned
    assert not a.has_weight_balance and not a.verified
    assert Aircraft.load().has_weight_balance and Aircraft.load().type_id == "cessna-182/182R"


def test_other_types_plan_a_trip(db, mins):
    from flightplanner.aircraft import catalog
    for tid in ("cessna-172/172N", "piper-pa28/PA-28-181", "piper-pa28/PA-28R", "cessna-182/182T"):
        a = Aircraft.from_type(tid)
        plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), a, mins)
        assert plans, tid
        ctx = _ctx(db, a, mins, plans)
        r = evaluate_plan(ctx, plans[0], _morning(ctx))
        assert r.legs and all(lr.fuel_at_landing_gal > 0 for lr in r.legs), tid
        for lr in r.legs:
            assert lr.fuel_used_gal < a.usable_gal, tid
    slow = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), Aircraft.from_type("cessna-172/172-172M"), mins)
    assert all(p.stops for p in slow), "a 35-gal 172 can't do KLEX-KSPG nonstop"


# --- FAA registry lookup --------------------------------------------------------
def test_tail_number_lookup_matches_the_catalog(fake_registry):
    R = fake_registry
    assert R.normalize_n(" n101-sp ") == "101SP"
    r = R.lookup("n101sp")
    assert r.n_number == "N101SP" and r.year == 1986 and r.type_id == "cessna-182/182R"
    assert r.description == "1986 Cessna 182R with a Continental O-470 SERIES, 230 hp"
    assert r.engine_note() is None
    assert R.lookup("N99999") is None and R.lookup("") is None


def test_tail_number_lookup_flags_engine_swaps_and_unknown_types(fake_registry):
    R = fake_registry
    conv = R.lookup("N1014F")
    assert conv.type_id == "cessna-172/172N" and "180 hp" in conv.engine_note()
    sr22 = R.lookup("N2RX")
    assert sr22.type_id is None and sr22.make == "Cirrus"
    twin = R.lookup("N77AB")
    assert twin.multi_engine and twin.year is None and twin.type_id is None


def test_registry_data_has_no_owner_columns():
    import gzip as _gz
    from flightplanner.config import DATA_DIR
    with _gz.open(DATA_DIR / "faa_aircraft.csv.gz", "rt") as f:
        assert f.readline().strip() == "n_number,year,model_code,engine_code"

def test_can_i_fly_today_tomorrow_weekend(db, ac, mins):
    from zoneinfo import ZoneInfo
    from flightplanner.weekend import NODATA, ask_days, can_i_fly
    assert ask_days(date(2026, 10, 22)) == [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 24),
                                            date(2026, 10, 25)]                       # Thu: + Fri, Sat, Sun
    assert ask_days(date(2026, 10, 24)) == [date(2026, 10, 24), date(2026, 10, 25)]  # Sat: today + Sun
    assert ask_days(date(2026, 10, 26))[:2] == [date(2026, 10, 26), date(2026, 10, 27)]
    assert len(ask_days(date(2026, 10, 26))) == 5                                    # Mon: + Fri, Sat, Sun
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)[:1]
    ctx = _ctx(db, ac, mins, plans)
    zone = ZoneInfo(ctx.tz)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    morning = datetime(day.year, day.month, day.day, 9, tzinfo=zone).astimezone(timezone.utc)
    evening = datetime(day.year, day.month, day.day, 20, tzinfo=zone).astimezone(timezone.utc)
    ans = can_i_fly(ctx.forecast, db.get("KLEX"), mins, ctx.tz, morning)
    assert [lbl for lbl, _ in ans][:2] == ["Today", "Tomorrow"]
    assert ans[0][1].verdict != NODATA and all(a.reason for _, a in ans)
    late = can_i_fly(ctx.forecast, db.get("KLEX"), mins, ctx.tz, evening)
    assert late[0][1].verdict == NODATA and "daylight" in late[0][1].reason

def test_airports_are_nationwide_with_time_zones(db):
    assert len(db.airports) > 4000
    assert db.get("KLEX").tz == "America/New_York"
    assert db.get("KBWG").tz == "America/Chicago"          # Bowling Green KY is Central
    assert db.get("KPHX").tz == "America/Phoenix"          # no daylight saving time
    assert db.get("KBOI").state == "ID" and db.get("KBOI").runways
    with pytest.raises(KeyError, match="US public airports"):
        db.get("KZZZ")



# --- day / night toggle ---------------------------------------------------------
def _evening_plan(db, ac, mins, dest, hour):
    plans = candidate_plans(db, db.get("KLEX"), db.get(dest), ac, mins)
    ctx = _ctx(db, ac, mins, plans)
    day = ctx.local(ctx.now).date() + timedelta(days=1)
    from zoneinfo import ZoneInfo
    dep = datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo(ctx.tz)).astimezone(timezone.utc)
    return ctx, evaluate_plan(ctx, plans[0], dep)


def test_night_allowed_is_not_a_no_go(db, ac):
    mins = load_minimums({"day_only": False, "night_current": True})
    ctx, r = _evening_plan(db, ac, mins, "KGGE", 18)
    assert not any(f.kind == "night" for f in r.findings)
    assert any(f.kind == "night-info" for f in r.findings)
    assert not any(f.kind == "night-currency" for f in r.findings)


def test_night_without_currency_is_flagged(db, ac):
    mins = load_minimums({"day_only": False, "night_current": False})
    ctx, r = _evening_plan(db, ac, mins, "KGGE", 19)
    assert any(f.kind == "night-currency" and "61.57" in f.text for f in r.findings)


def test_night_over_the_mountains_is_cautioned(db, ac):
    mins = load_minimums({"day_only": False, "night_current": True})
    ctx, r = _evening_plan(db, ac, mins, "KGGE", 20)    # KLEX-KGGE crosses the Appalachians
    assert any(f.kind == "night-terrain" and f.level == "CAUTION" for f in r.findings)


def test_night_reserve_is_at_least_45_minutes(db, ac):
    from flightplanner.planner import _is_night
    mins = load_minimums({"day_only": False, "night_current": True, "reserve_minutes": 30})
    ctx, r = _evening_plan(db, ac, mins, "KGGE", 20)
    lr = r.legs[-1]
    assert _is_night(ctx, lr.leg.arr, lr.arr_time)
    assert lr.reserve_required_gal == pytest.approx(45 / 60 * ac.cruise_gph, rel=0.01)
    day_mins = load_minimums({"day_only": False, "night_current": True, "reserve_minutes": 30})
    ctx2, r2 = _evening_plan(db, ac, day_mins, "KGGE", 9)              # same rules, daytime
    assert r2.legs[-1].reserve_required_gal == pytest.approx(30 / 60 * ac.cruise_gph, rel=0.01)


# --- fuel availability (FAA NASR) ---------------------------------------------
def _nasr_zip(rows_base, rows_att):
    import csv as _csv, io as _io, zipfile as _zip
    def to_csv(rows):
        buf = _io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
        return buf.getvalue()
    out = _io.BytesIO()
    with _zip.ZipFile(out, "w") as z:
        z.writestr("APT_BASE.csv", to_csv(rows_base))
        z.writestr("APT_ATT.csv", to_csv(rows_att))
    return out.getvalue()


def _base(site, arpt, icao, fuel, use="PU", typ="A"):
    return {"EFF_DATE": "2026/10/01", "SITE_NO": site, "SITE_TYPE_CODE": typ, "STATE_CODE": "GA",
            "ARPT_ID": arpt, "ICAO_ID": icao, "FACILITY_USE_CODE": use, "FUEL_TYPES": fuel}


def test_build_fuel_data_parses_nasr(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bfd", Path(__file__).parents[1] / "scripts" / "build_fuel_data.py")
    bfd = importlib.util.module_from_spec(spec); spec.loader.exec_module(bfd)
    z = _nasr_zip([_base("1.", "MCN", "KMCN", "100LL,A"), _base("2.", "GA9", "", ""),
                   _base("3.", "PVT", "", "100LL", use="PR"), _base("4.", "HEL", "", "", typ="H")],
                  [{"SITE_NO": "1.", "SKED_SEQ_NO": "1", "MONTH": "ALL", "DAY": "ALL", "HOUR": "0600-2200"}])
    rows = bfd.build_rows(z)
    assert [r["ident"] for r in rows] == ["GA9", "KMCN"]          # private + heliport dropped
    mcn = next(r for r in rows if r["ident"] == "KMCN")
    assert mcn["fuel_types"] == "100LL,A" and "0600-2200" in mcn["attendance"]
    from datetime import date as _d
    assert bfd.cycle_dates(_d(2026, 10, 28))[0] == _d(2026, 10, 1)
    assert bfd.cycle_dates(_d(2026, 10, 29))[0] == _d(2026, 10, 29)
    assert bfd.cycle_url(_d(2026, 10, 1)).endswith("/01_Oct_2026_APT_CSV.zip")


def test_stops_skip_airports_without_avgas(db, ac, mins, monkeypatch, tmp_path):
    import flightplanner.fuel as fuel
    plans = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
    stops = {s.ident for p in plans for s in p.stops}
    assert stops
    banned = sorted(stops)[0]
    f = tmp_path / "fuel.csv"
    lines = ["ident,faa_id,fuel_types,attendance,eff_date"]
    for ap in db.airports.values():
        lines.append(f"{ap.ident},{ap.ident[1:] if ap.ident.startswith('K') else ap.ident},"
                     f"{'A' if ap.ident == banned else '100LL'},,2026/10/01")
    f.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(fuel, "FUEL_FILE", f)
    fuel.load_fuel.cache_clear()
    try:
        assert db.get(banned).avgas is False and db.get("KLEX").avgas is True
        plans2 = candidate_plans(db, db.get("KLEX"), db.get("KSPG"), ac, mins)
        assert banned not in {s.ident for p in plans2 for s in p.stops}
    finally:
        fuel.load_fuel.cache_clear()


def test_fuel_unknown_without_data(monkeypatch, tmp_path):
    import flightplanner.fuel as fuel
    monkeypatch.setattr(fuel, "FUEL_FILE", tmp_path / "missing.csv")
    fuel.load_fuel.cache_clear()
    try:
        assert fuel.avgas_status("KMCN") is None
        assert AirportDB.load().get("KMCN").known_fuel is True       # falls back to the built-in list
    finally:
        fuel.load_fuel.cache_clear()


def test_daily_outlook_checks_crosswind_with_gusts(db, ac, mins):
    from flightplanner.outlook import airport_outlook
    from flightplanner.weekend import can_i_fly
    plans = candidate_plans(db, db.get("KLEX"), db.get("KGGE"), ac, mins)[:1]
    ctx = _ctx(db, ac, mins, plans)
    # 330@12G20 at KLEX: under the wind (25) and gust (25) limits, but ~18 kt across rwy 27 with the gust
    for ser in ctx.forecast.series.values():
        h = ser["hourly"]
        n = len(h["time"])
        h["wind_direction_10m"], h["wind_speed_10m"], h["wind_gusts_10m"] = [330] * n, [12] * n, [20] * n
        h["weather_code"], h["precipitation_probability"], h["cloud_cover_low"] = [0] * n, [0] * n, [0] * n
        h["cloud_cover_mid"], h["visibility"], h["cape"] = [0] * n, [30000] * n, [0] * n
    ctx.forecast._cache.clear()
    days = airport_outlook(ctx.forecast, db.get("KLEX"), mins, ctx.tz, days=3)
    full = [d for d in days if d.covered_hours == d.daylight_hours][0]
    assert full.verdict == "Poor" and full.reasons[0].startswith("Crosswind")
    assert any("rwy 27" in t for h in full.hours for _, t in h.problems)
    tomorrow = can_i_fly(ctx.forecast, db.get("KLEX"), mins, ctx.tz, _morning(ctx))[1][1]
    assert tomorrow.verdict == "NO-GO" and "Crosswind" in tomorrow.reason


def test_minimum_presets_stay_above_legal_floors():
    from flightplanner.config import minimum_presets, preset_minimums
    presets = minimum_presets()
    assert list(presets) == ["cautious", "standard", "experienced"]
    prev = None
    for name in presets:
        m = preset_minimums(name)
        assert m.airport_min_ceiling_ft_agl >= 1000 and m.airport_min_vis_sm >= 3       # 14 CFR 91.155
        assert m.reserve_minutes >= 30                                                    # 14 CFR 91.151
        assert m.max_crosswind_kt <= 15                                                   # C182 demonstrated
        if prev:   # each step is no stricter than the one before
            assert m.airport_min_ceiling_ft_agl <= prev.airport_min_ceiling_ft_agl
            assert m.max_crosswind_kt >= prev.max_crosswind_kt
        prev = m
    assert preset_minimums("cautious", {"max_crosswind_kt": 8}).max_crosswind_kt == 8


def test_airport_search_puts_the_obvious_one_first(db):
    assert db.search("lexington")[0].ident == "KLEX"
    assert db.search("klex")[0].ident == "KLEX" and db.search("spg")[0].ident == "KSPG"
