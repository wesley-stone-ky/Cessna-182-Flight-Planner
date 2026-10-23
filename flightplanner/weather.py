"""Weather data: 7-day NWS forecast grids (api.weather.gov) + official aviation
products from aviationweather.gov (METAR, TAF, AIRMET/SIGMET, G-AIRMET).

The NWS grid forecast is what makes multi-day trip-day screening possible; it
is not an aviation product. Inside ~30 hours the planner overlays TAFs and
advisories from the Aviation Weather Center, which ARE official products.
All sources are US government data: free, no API key, fine for commercial use.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from .config import CACHE_DIR
from .geo import point_in_polygon

USER_AGENT = "c182-trip-planner/0.1 (personal flight planning; github)"
AWC_BASE = "https://aviationweather.gov/api/data"

# Pressure levels a series may carry (synthetic demo data has all three; NWS
# grids only get an estimated 850 hPa wind, see nws_series).
LEVELS_HPA = (925, 850, 700)   # ~2,500 / 5,000 / 10,000 ft
CLOUD_LEVELS_HPA = (925, 850, 700)
STD_LAPSE_C_PER_FT = 2.0 / 1000   # standard atmosphere

GRID = 0.5  # degrees (~30 nm); route points are snapped to this grid to share forecasts
M_TO_FT = 3.28084
THUNDER_CODES = {95, 96, 99}
WMO_TEXT = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}


def snap(lat: float, lon: float) -> tuple[float, float]:
    return (round(round(lat / GRID) * GRID, 3), round(round(lon / GRID) * GRID, 3))


def to_utc(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    s = str(v).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Snapshot of model weather at one point / hour
# ---------------------------------------------------------------------------
@dataclass
class Level:
    height_ft_msl: float
    wind_dir: float
    wind_kt: float
    temp_c: float
    rh: float


@dataclass
class WxPoint:
    time: datetime
    lat: float
    lon: float
    surface_elev_ft: float
    temp_c: float | None
    dew_c: float | None
    low_cloud: float
    mid_cloud: float
    vis_sm: float | None
    precip_prob: float
    precip_mm: float
    wcode: int
    cape: float
    freezing_level_ft: float | None
    wind_dir: float
    wind_kt: float
    gust_kt: float
    levels: list[Level] = field(default_factory=list)
    cloud_layers: list[tuple[float, float]] = field(default_factory=list)  # (height ft MSL, cover %)
    # From the National Weather Service grid (when available)
    nws: bool = False
    nws_ceiling_ft: float | None = None
    sky_cover: float | None = None
    thunder_prob: float | None = None
    aloft_estimated: bool = False   # winds/temps aloft are stand-ins (NWS grids have none)

    @property
    def thunder(self) -> bool:
        return self.wcode in THUNDER_CODES or (self.thunder_prob or 0) >= 40

    def storm_level(self, caution_cape: float, nogo_cape: float) -> str:
        """'likely' / 'some' / 'low' from NWS thunder chance and/or model CAPE."""
        tp = self.thunder_prob or 0
        if self.thunder or (self.cape >= nogo_cape and self.precip_prob >= 30):
            return "likely"
        if tp >= 15 or (self.cape >= caution_cape and self.precip_prob >= 30):
            return "some"
        return "low"

    def storm_detail(self) -> str:
        bits = []
        if self.thunder_prob is not None:
            bits.append(f"{self.thunder_prob:.0f}% thunder chance")
        if self.cape:
            bits.append(f"CAPE {self.cape:.0f}")
        bits.append(f"{self.precip_prob:.0f}% precip")
        return ", ".join(bits)

    @property
    def wx_text(self) -> str:
        return WMO_TEXT.get(self.wcode, f"code {self.wcode}")

    def ceiling_agl_ft(self) -> float | None:
        """Estimated ceiling (lowest broken/overcast base) in ft AGL, or None.

        The NWS grid forecasts ceiling height directly; we use it when present.
        Otherwise (synthetic demo data) we combine low-cloud cover with the
        surface temperature/dewpoint spread (~400 ft per deg C, the classic
        convective-base rule) and saturated pressure levels.
        This is a SCREENING estimate. TAFs override it near the airports.
        """
        if self.nws:
            if self.nws_ceiling_ft is not None and (self.sky_cover is None or self.sky_cover >= 50):
                return max(100.0, self.nws_ceiling_ft)
            if self.sky_cover is not None and self.sky_cover < 50:
                return None
            if self.sky_cover is not None and self.sky_cover >= 70 and not self.cloud_layers \
                    and self.low_cloud < 60:
                # overcast with no NWS ceiling and no model layers: use the dewpoint-spread estimate
                spread = (self.temp_c - self.dew_c) if (self.temp_c is not None and self.dew_c is not None) else 7.5
                return max(300.0, min(9800.0, max(0.0, spread) * 400.0))
        agl_layers = sorted((h - self.surface_elev_ft, c) for h, c in self.cloud_layers
                            if h - self.surface_elev_ft > 0)
        layer_base = next((h for h, c in agl_layers if c >= 60 and h <= 10000), None)
        if self.low_cloud >= 60 or (layer_base is not None and layer_base < 9800):
            if self.low_cloud < 60 and layer_base is not None:
                return max(200.0, layer_base)
            base = 3000.0
            if self.temp_c is not None and self.dew_c is not None:
                base = max(0.0, self.temp_c - self.dew_c) * 400.0
            sat = [lv.height_ft_msl - self.surface_elev_ft for lv in self.levels
                   if lv.rh >= 92 and lv.height_ft_msl - self.surface_elev_ft > 0]
            if sat:
                base = min(base, min(sat)) if base > 500 else base
            if layer_base is not None:
                base = min(base, layer_base)
            if self.vis_sm is not None and self.vis_sm < 1 and self.wcode in (45, 48):
                base = min(base, 200.0)
            return max(200.0, min(base, 9800.0))
        if self.mid_cloud >= 70:
            return 10000.0
        return None

    def wind_at(self, alt_ft_msl: float) -> tuple[float, float, float | None, float | None]:
        """(wind_from_deg, wind_kt, temp_c, rh) interpolated to an altitude."""
        pts = sorted(self.levels, key=lambda l: l.height_ft_msl)
        surf = Level(self.surface_elev_ft + 33, self.wind_dir, self.wind_kt,
                     self.temp_c if self.temp_c is not None else 15.0, 70.0)
        pts = [surf] + [p for p in pts if p.height_ft_msl > surf.height_ft_msl]
        if not pts:
            return 0.0, 0.0, None, None
        if alt_ft_msl <= pts[0].height_ft_msl:
            p = pts[0]
            return p.wind_dir, p.wind_kt, p.temp_c, p.rh
        for lo, hi in zip(pts, pts[1:]):
            if alt_ft_msl <= hi.height_ft_msl:
                t = (alt_ft_msl - lo.height_ft_msl) / max(1.0, hi.height_ft_msl - lo.height_ft_msl)
                # interpolate wind as vectors
                def uv(l: Level):
                    r = math.radians(l.wind_dir)
                    return -l.wind_kt * math.sin(r), -l.wind_kt * math.cos(r)
                u0, v0 = uv(lo)
                u1, v1 = uv(hi)
                u, v = u0 + t * (u1 - u0), v0 + t * (v1 - v0)
                spd = math.hypot(u, v)
                wdir = (math.degrees(math.atan2(-u, -v)) + 360) % 360
                return wdir, spd, lo.temp_c + t * (hi.temp_c - lo.temp_c), lo.rh + t * (hi.rh - lo.rh)
        p = pts[-1]
        return p.wind_dir, p.wind_kt, p.temp_c, p.rh


# ---------------------------------------------------------------------------
# Gridded multi-day forecast
# ---------------------------------------------------------------------------
class GridForecast:
    """Hourly model forecast for a set of snapped grid points."""

    def __init__(self, series: dict[tuple[float, float], dict], fetched_at: datetime | None = None):
        self.series = series
        self.fetched_at = fetched_at or datetime.now(timezone.utc)
        self._cache: dict = {}
        self._times: dict[tuple[float, float], list[datetime]] = {
            k: [to_utc(t) for t in s["hourly"]["time"]] for k, s in series.items()
        }

    @property
    def horizon(self) -> datetime | None:
        ends = [ts[-1] for ts in self._times.values() if ts]
        return min(ends) if ends else None

    def _key(self, lat: float, lon: float) -> tuple[float, float] | None:
        k = snap(lat, lon)
        if k in self.series:
            return k
        if not self.series:
            return None
        return min(self.series, key=lambda kk: (kk[0] - lat) ** 2 + (kk[1] - lon) ** 2)

    def at(self, lat: float, lon: float, when: datetime) -> WxPoint | None:
        k = self._key(lat, lon)
        if k is None:
            return None
        times = self._times[k]
        if not times or when < times[0] - timedelta(hours=1) or when > times[-1] + timedelta(hours=1):
            return None
        i = bisect_left(times, when)
        if i >= len(times):
            i = len(times) - 1
        elif i > 0 and (when - times[i - 1]) < (times[i] - when):
            i -= 1
        ck = (k, i)
        if ck in self._cache:
            return self._cache[ck]
        s = self.series[k]
        h = s["hourly"]

        def g(name: str, default=None):
            arr = h.get(name)
            if not arr or i >= len(arr) or arr[i] is None:
                return default
            return arr[i]

        levels = []
        for p in LEVELS_HPA:
            z = g(f"geopotential_height_{p}hPa")
            if z is None:
                continue
            levels.append(Level(
                height_ft_msl=float(z) * M_TO_FT,
                wind_dir=float(g(f"wind_direction_{p}hPa", 0.0)),
                wind_kt=float(g(f"wind_speed_{p}hPa", 0.0)),
                temp_c=float(g(f"temperature_{p}hPa", 0.0)),
                rh=float(g(f"relative_humidity_{p}hPa", 50.0)),
            ))
        layers = []
        for p in CLOUD_LEVELS_HPA:
            z, cc = g(f"geopotential_height_{p}hPa"), g(f"cloud_cover_{p}hPa")
            if z is not None and cc is not None:
                layers.append((float(z) * M_TO_FT, float(cc)))
        vis = g("visibility")
        fzl = g("freezing_level_height")
        nws_has = g("nws_has") is not None
        nws_cig = g("nws_ceiling_m")
        aloft_est = g("aloft_estimated") is not None
        sfc_ft = float(s.get("elevation") or 0) * M_TO_FT
        fzl_ft = (float(fzl) * M_TO_FT) if fzl is not None else None
        t2 = g("temperature_2m")
        if fzl_ft is None and aloft_est and t2 is not None:
            # standard lapse rate from the surface temperature; marked as an estimate where shown
            fzl_ft = sfc_ft + max(0.0, float(t2)) / STD_LAPSE_C_PER_FT
        wp = WxPoint(
            time=times[i], lat=k[0], lon=k[1],
            surface_elev_ft=sfc_ft,
            temp_c=g("temperature_2m"), dew_c=g("dew_point_2m"),
            low_cloud=float(g("cloud_cover_low", g("sky_cover", 0)) or 0), mid_cloud=float(g("cloud_cover_mid", 0) or 0),
            vis_sm=(float(vis) / 1609.34) if vis is not None else None,
            precip_prob=float(g("precipitation_probability", 0) or 0),
            precip_mm=float(g("precipitation", 0) or 0),
            wcode=int(g("weather_code", 0) or 0), cape=float(g("cape", 0) or 0),
            freezing_level_ft=fzl_ft,
            wind_dir=float(g("wind_direction_10m", 0) or 0), wind_kt=float(g("wind_speed_10m", 0) or 0),
            gust_kt=float(g("wind_gusts_10m", 0) or 0), levels=levels, cloud_layers=layers,
            nws=nws_has, nws_ceiling_ft=(float(nws_cig) * M_TO_FT) if nws_cig is not None else None,
            sky_cover=g("sky_cover"), thunder_prob=g("thunder_prob"), aloft_estimated=aloft_est,
        )
        self._cache[ck] = wp
        return wp


def _cache_get(key: str, ttl_s: int):
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl_s:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _cache_put(key: str, data) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(json.dumps(data))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Official products from aviationweather.gov
# ---------------------------------------------------------------------------
@dataclass
class TafPeriod:
    start: datetime
    end: datetime
    change: str            # "", FM, BECMG, TEMPO, PROB
    wind_dir: float | None
    wind_kt: float | None
    gust_kt: float | None
    vis_sm: float | None
    ceiling_ft: float | None
    wx: str


@dataclass
class Advisory:
    kind: str              # SIGMET / AIRMET / G-AIRMET / CONVECTIVE SIGMET
    hazard: str
    valid_from: datetime | None
    valid_to: datetime | None
    polygon: list[tuple[float, float]]
    base_ft: float | None
    top_ft: float | None
    raw: str = ""

    def active(self, when: datetime) -> bool:
        if self.valid_from and when < self.valid_from:
            return False
        if self.valid_to and when > self.valid_to:
            return False
        return True

    def covers(self, lat: float, lon: float, when: datetime, alt_ft: float | None = None) -> bool:
        if not self.active(when) or not point_in_polygon(lat, lon, self.polygon):
            return False
        if alt_ft is not None:
            if self.base_ft is not None and alt_ft < self.base_ft - 500:
                return False
            if self.top_ft is not None and alt_ft > self.top_ft + 500:
                return False
        return True


def _vis(v) -> float | None:
    if v is None or v == "":
        return None
    s = str(v).replace("+", "").replace("SM", "").strip()
    try:
        if " " in s:  # "1 1/2"
            w, fr = s.split()
            n, d = fr.split("/")
            return float(w) + float(n) / float(d)
        if "/" in s:
            n, d = s.split("/")
            return float(n) / float(d)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def _ceiling(clouds) -> float | None:
    best = None
    for c in clouds or []:
        cover = str(c.get("cover", "")).upper()
        base = c.get("base")
        if cover in ("BKN", "OVC", "VV", "OVX") and base is not None:
            b = float(base)  # AWC JSON gives cloud bases in feet AGL
            best = b if best is None else min(best, b)
    return best


@dataclass
class Pirep:
    time: datetime | None
    lat: float
    lon: float
    alt_ft: float | None
    aircraft: str
    turb: str
    icing: str
    wx: str
    raw: str
    urgent: bool = False


@dataclass
class SourceStatus:
    name: str
    ok: bool = True
    count: int = 0
    fetched_at: datetime | None = None
    error: str = ""


def flight_category(ceiling_ft: float | None, vis_sm: float | None) -> str:
    """FAA flight categories (AIM 7-1): VFR / MVFR / IFR / LIFR."""
    c = 99999 if ceiling_ft is None else ceiling_ft
    v = 99 if vis_sm is None else vis_sm
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


def decode_metar(m: dict) -> dict:
    """Pull the fields a pilot looks at first out of an AWC METAR JSON record."""
    wd = m.get("wdir")
    cig = _ceiling(m.get("clouds"))
    vis = _vis(m.get("visib"))
    return {
        "station": m.get("icaoId", ""),
        "time": to_utc(m.get("obsTime")) or to_utc(m.get("reportTime")),
        "category": m.get("fltCat") or flight_category(cig, vis),
        "wind_dir": None if isinstance(wd, str) else wd,
        "wind_text": ("VRB" if isinstance(wd, str) else f"{int(wd or 0):03d}") + f"@{m.get('wspd', 0)}"
                     + (f"G{m['wgst']}" if m.get("wgst") else "") + " kt",
        "wind_kt": m.get("wspd"),
        "gust_kt": m.get("wgst"),
        "vis_sm": vis,
        "ceiling_ft": cig,
        "clouds": " ".join(f"{c.get('cover', '')}{int(c['base'] / 100):03d}" if c.get("base") is not None
                           else str(c.get("cover", "")) for c in (m.get("clouds") or [])) or m.get("cover", ""),
        "temp_c": m.get("temp"),
        "dew_c": m.get("dewp"),
        "altim_inhg": round(float(m["altim"]) * 0.02953, 2) if m.get("altim") else None,
        "wx": m.get("wxString") or "",
        "raw": m.get("rawOb", ""),
        "lat": m.get("lat"),
        "lon": m.get("lon"),
    }


class AviationWeather:
    """Client for the aviationweather.gov Data API (limit: 100 requests/min).

    Small airports often have no TAF (KGGE, KSPG and KTPF don't). For those we
    borrow the nearest TAF within ~40 nm and say which one we used.
    """

    def __init__(self, session: requests.Session | None = None, ttl_s: int = 300):
        self.sess = session or requests.Session()
        try:
            self.sess.headers.update({"User-Agent": USER_AGENT})
        except AttributeError:
            pass
        self.ttl = ttl_s
        self.errors: list[str] = []
        self.status: dict[str, SourceStatus] = {}

    def _get(self, path: str, params: dict, label: str | None = None):
        label = label or path
        st = self.status.setdefault(label, SourceStatus(label))
        ck = "awc_" + hashlib.sha1((path + json.dumps(params, sort_keys=True)).encode()).hexdigest()[:16]
        cached = _cache_get(ck, self.ttl)
        if cached is not None:
            st.fetched_at = st.fetched_at or _cache_time(ck)
            return cached
        try:
            r = self.sess.get(f"{AWC_BASE}/{path}", params=params, timeout=30)
            if r.status_code == 204 or not r.content:
                data = []
            else:
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # network / parse problems are reported, not fatal
            st.ok = False
            st.error = str(e)[:200]
            self.errors.append(f"{label}: {e}")
            return []
        st.fetched_at = datetime.now(timezone.utc)
        _cache_put(ck, data)
        return data

    # -- METAR / TAF ---------------------------------------------------------
    def metars(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        data = self._get("metar", {"ids": ",".join(ids), "format": "json", "hours": 3}, "METARs")
        out: dict[str, dict] = {}
        for m in data if isinstance(data, list) else []:
            sid = m.get("icaoId") or m.get("stationId")
            if sid and sid not in out:   # newest first
                out[sid] = m
        self.status["METARs"].count = len(out)
        return out

    def tafs(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        data = self._get("taf", {"ids": ",".join(ids), "format": "json"}, "TAFs")
        out: dict[str, dict] = {}
        for t in data if isinstance(data, list) else []:
            sid = t.get("icaoId") or t.get("stationId")
            if not sid or sid in out:
                continue
            periods = []
            for f in t.get("fcsts", []) or []:
                start, end = to_utc(f.get("timeFrom")), to_utc(f.get("timeTo"))
                if not start or not end:
                    continue
                change = str(f.get("fcstChange") or "").upper()
                if f.get("probability"):
                    change = "PROB"
                wd = f.get("wdir")
                periods.append(TafPeriod(
                    start, end, change,
                    float(wd) if isinstance(wd, (int, float)) else None,
                    float(f["wspd"]) if f.get("wspd") is not None else None,
                    float(f["wgst"]) if f.get("wgst") is not None else None,
                    _vis(f.get("visib")), _ceiling(f.get("clouds")), str(f.get("wxString") or "")))
            out[sid] = {"raw": t.get("rawTAF", ""), "periods": periods, "station": sid,
                        "issued": to_utc(t.get("issueTime")), "lat": t.get("lat"), "lon": t.get("lon")}
        self.status["TAFs"].count = len(out)
        return out

    def for_airports(self, airports, nearby, max_nm: float = 40.0):
        """METAR + TAF for each airport, borrowing the nearest station's when missing.

        `nearby(airport)` returns candidate (ident, distance_nm) pairs, nearest first.
        Returns (metars, tafs) keyed by the ROUTE airport ident; each record has
        'station' and 'dist_nm' so the UI can say "TAF from KMYR, 27 nm".
        """
        ids = [a.ident for a in airports]
        cand: dict[str, list[tuple[str, float]]] = {
            a.ident: [(i, d) for i, d in nearby(a) if d <= max_nm][:8] for a in airports}
        all_ids = sorted(set(ids) | {i for lst in cand.values() for i, _ in lst})
        metars = self.metars(all_ids)
        tafs = self.tafs(all_ids)
        m_out, t_out = {}, {}
        for a in airports:
            options = [(a.ident, 0.0)] + cand[a.ident]
            for sid, d in options:
                if sid in metars:
                    m_out[a.ident] = dict(metars[sid], _station=sid, _dist_nm=d)
                    break
            for sid, d in options:
                if sid in tafs:
                    t_out[a.ident] = dict(tafs[sid], station=sid, dist_nm=d)
                    break
        return m_out, t_out

    # -- Advisories / PIREPs ------------------------------------------------
    def advisories(self) -> list[Advisory]:
        out: list[Advisory] = []
        sig = self._get("airsigmet", {"format": "json"}, "AIRMET/SIGMET") or []
        for item in sig:
            poly = [(float(c["lat"]), float(c["lon"])) for c in item.get("coords", []) or []
                    if "lat" in c and "lon" in c]
            if len(poly) < 3:
                continue
            kind = str(item.get("airSigmetType", "SIGMET")).upper()
            hazard = str(item.get("hazard", "")).upper()
            if hazard.startswith("CONV"):
                kind = "CONVECTIVE SIGMET"
            out.append(Advisory(kind, hazard, to_utc(item.get("validTimeFrom")), to_utc(item.get("validTimeTo")),
                                poly, _num(item.get("altitudeLow1")), _num(item.get("altitudeHi1")),
                                str(item.get("rawAirSigmet", ""))))
        self.status["AIRMET/SIGMET"].count = len(out)
        g = self._get("gairmet", {"format": "json"}, "G-AIRMETs") or []
        n = 0
        for item in g:
            poly = [(float(c["lat"]), float(c["lon"])) for c in item.get("coords", []) or []
                    if "lat" in c and "lon" in c]
            if len(poly) < 3:
                continue
            vt = to_utc(item.get("validTime"))
            n += 1
            out.append(Advisory("G-AIRMET", str(item.get("hazard", "")).upper(),
                                vt - timedelta(hours=1, minutes=30) if vt else None,
                                vt + timedelta(hours=1, minutes=30) if vt else None,
                                poly, _alt(item.get("base")), _alt(item.get("top")),
                                str(item.get("due_to") or item.get("dueTo") or "")))
        self.status["G-AIRMETs"].count = n
        return out

    def pireps(self, points: list[tuple[str, float]], age_h: int = 2) -> list[Pirep]:
        """PIREPs within `distance` nm of each (station_ident, distance_nm)."""
        seen, out = set(), []
        for sid, dist in points:
            data = self._get("pirep", {"id": sid, "distance": int(dist), "age": age_h, "format": "json"},
                             "PIREPs") or []
            for p in data:
                raw = p.get("rawOb", "")
                if raw in seen:
                    continue
                seen.add(raw)
                try:
                    lat, lon = float(p.get("lat")), float(p.get("lon"))
                except (TypeError, ValueError):
                    continue
                fl = _num(p.get("fltLvl"))
                turb = " ".join(x for x in (p.get("tbInt1"), p.get("tbType1")) if x)
                ice = " ".join(x for x in (p.get("icgInt1"), p.get("icgType1")) if x)
                out.append(Pirep(to_utc(p.get("obsTime")), lat, lon, fl * 100 if fl is not None else None,
                                 str(p.get("acType") or ""), turb, ice, str(p.get("wxString") or ""), raw,
                                 str(p.get("pirepType", "")).upper() == "URGENT PIREP" or " UUA " in raw))
        if "PIREPs" in self.status:
            self.status["PIREPs"].count = len(out)
        return out


def _cache_time(key: str) -> datetime | None:
    path = CACHE_DIR / f"{key}.json"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _alt(v) -> float | None:
    """G-AIRMET base/top come as hundreds of feet or 'SFC'/'FZL'."""
    if v is None:
        return None
    s = str(v).upper()
    if s == "SFC":
        return 0.0
    try:
        n = float(s)
        return n * 100 if n < 700 else n
    except ValueError:
        return None


def taf_at(taf: dict | None, when: datetime) -> tuple[TafPeriod | None, list[TafPeriod]]:
    """Prevailing TAF period at `when`, plus any TEMPO/PROB groups covering it."""
    if not taf:
        return None, []
    prevailing = None
    temps = []
    for p in taf["periods"]:
        if not (p.start <= when < p.end):
            continue
        if p.change in ("TEMPO", "PROB"):
            temps.append(p)
        else:
            prevailing = p
    return prevailing, temps


# ---------------------------------------------------------------------------
# Forecast: NWS grids, 7 days
# ---------------------------------------------------------------------------
NWS_VARS = ("temperature_2m", "dew_point_2m", "visibility", "precipitation_probability",
            "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "weather_code",
            "nws_ceiling_m", "sky_cover", "thunder_prob", "nws_has")


def nws_series(nws: dict) -> dict:
    """GridForecast series for one NWS gridpoint.

    NWS grids have no winds or humidity aloft. The mixed-layer ("transport")
    wind stands in for ~5,000 ft so groundspeed isn't computed from surface
    wind, and every hour is flagged `aloft_estimated` so the fuel and icing
    checks know to be conservative.
    """
    times = nws["times"]
    v = nws["vars"]
    n = len(times)
    hourly: dict[str, list] = {"time": times}
    for name in NWS_VARS:
        hourly[name] = v.get(name) or [None] * n
    elev = nws.get("elevation_m") or 0
    tw, td = v.get("transport_wind_kt") or [None] * n, v.get("transport_wind_dir") or [None] * n
    hourly["wind_speed_850hPa"] = tw
    hourly["wind_direction_850hPa"] = td
    hourly["geopotential_height_850hPa"] = [elev + 1500 if x is not None else None for x in tw]
    hourly["temperature_850hPa"] = [(t - 9.75) if t is not None else None for t in hourly["temperature_2m"]]
    hourly["aloft_estimated"] = [1] * n
    return {"elevation": elev, "hourly": hourly}


def fetch_forecast(points: Iterable[tuple[float, float]], session: requests.Session | None = None,
                   ttl_s: int = 1800) -> tuple[GridForecast, dict]:
    """Returns (forecast, status) where status maps source name -> SourceStatus."""
    from .nws import NWS
    keys = sorted({snap(la, lo) for la, lo in points})
    st = SourceStatus("NWS forecast (weather.gov)")
    series: dict = {}
    try:
        res = NWS(session=session, ttl_s=ttl_s).fetch(keys)
        series = res.series
        st.count, st.fetched_at = res.ok_points, datetime.now(timezone.utc)
        if not res.ok_points:
            st.ok, st.error = False, (res.errors[0] if res.errors else "no data returned")
    except Exception as e:
        st.ok, st.error = False, str(e)[:200]
    if not series:
        raise RuntimeError(f"The NWS forecast didn't answer: {st.error}")
    return GridForecast({k: nws_series(v) for k, v in series.items()}), {st.name: st}
