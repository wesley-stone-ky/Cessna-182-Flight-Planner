"""National Weather Service forecast grids (api.weather.gov).

Free, no API key, built for apps, and not rate-limited the way shared free
APIs are. Each 2.5 km gridpoint carries a 7-day forecast with what a VFR pilot
cares about most: sky cover, CEILING HEIGHT, VISIBILITY, wind and gusts,
precipitation chance, thunder chance and the weather type.

Raw layers come as ISO-8601 intervals ("2026-10-23T02:00:00+00:00/PT3H");
we expand them to hourly arrays in the same shape as the Open-Meteo data so
the rest of the app doesn't care which source a number came from.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from .config import CACHE_DIR

NWS_BASE = "https://api.weather.gov"
NWS_UA = "(c182-trip-planner, personal flight planning)"
_POINTS_FILE = CACHE_DIR / "nws_points.json"
_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")

THUNDER_COVERAGE = {"chance", "likely", "scattered", "numerous", "definite", "occasional", "frequent",
                    "widespread", "areas"}


@dataclass
class NwsResult:
    series: dict = field(default_factory=dict)   # key -> {"elevation_m", "times", "vars"}
    ok_points: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_interval(s: str) -> tuple[datetime, int] | None:
    """'2026-10-23T02:00:00+00:00/PT3H' -> (start, hours)."""
    try:
        start_s, dur_s = s.split("/")
        start = datetime.fromisoformat(start_s.replace("Z", "+00:00")).astimezone(timezone.utc)
        m = _DUR.fullmatch(dur_s)
        if not m:
            return None
        d, h, mi = (int(x) if x else 0 for x in m.groups())
        hours = max(1, d * 24 + h + (1 if mi >= 30 else 0))
        return start, hours
    except (ValueError, AttributeError):
        return None


def _convert(value, uom: str | None):
    if value is None:
        return None
    u = (uom or "").split(":")[-1]
    v = float(value)
    if u == "km_h-1":
        return v * 0.539957
    if u == "m_s-1":
        return v * 1.943844
    if u == "degF":
        return (v - 32) * 5 / 9
    if u == "km":
        return v * 1000
    if u in ("ft", "[ft_i]"):
        return v * 0.3048
    if u in ("mi", "[mi_i]"):
        return v * 1609.34
    return v


def weather_code(items) -> int | None:
    """Map the NWS 'weather' layer to the WMO-style codes used elsewhere."""
    if not items:
        return None
    best = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        w = str(it.get("weather") or "")
        cov = str(it.get("coverage") or "")
        if not w:
            continue
        if w == "thunderstorms":
            code = 95 if cov in THUNDER_COVERAGE else 0
        elif w in ("freezing_rain", "freezing_drizzle"):
            code = 66
        elif w in ("snow", "snow_showers", "sleet", "blowing_snow"):
            code = 71
        elif w in ("rain_showers",):
            code = 80 if cov != "slight_chance" else 0
        elif w in ("rain", "drizzle"):
            code = 61 if cov != "slight_chance" else 0
        elif w in ("fog", "freezing_fog"):
            code = 45
        else:
            code = 0
        best = max(best, code)
    return best


def _layer_hourly(layer: dict | None, axis: dict[int, int], n: int, convert: bool = True) -> list:
    out = [None] * n
    if not layer:
        return out
    uom = layer.get("uom")
    for item in layer.get("values", []) or []:
        iv = _parse_interval(str(item.get("validTime", "")))
        if not iv:
            continue
        start, hours = iv
        val = item.get("value")
        if convert:
            val = _convert(val, uom)
        t0 = int(start.timestamp()) // 3600 * 3600
        for h in range(hours):
            idx = axis.get(t0 + h * 3600)
            if idx is not None:
                out[idx] = val
    return out


class NWS:
    def __init__(self, session: requests.Session | None = None, ttl_s: int = 1800, workers: int = 6):
        self.sess = session or requests.Session()
        self.ttl = ttl_s
        self.workers = workers
        try:
            self.points = json.loads(_POINTS_FILE.read_text())
        except Exception:
            self.points = {}

    def _get(self, url: str):
        last = None
        for attempt in range(3):
            try:
                r = self.sess.get(url, timeout=30, headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"})
            except Exception as e:
                last = str(e)
                time.sleep(1 + attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2 + 2 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(last or "NWS request failed")

    def grid_url(self, lat: float, lon: float) -> str | None:
        k = f"{lat:.3f},{lon:.3f}"
        if k in self.points:
            return self.points[k] or None
        data = self._get(f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
        url = ((data or {}).get("properties") or {}).get("forecastGridData") or ""
        self.points[k] = url
        return url or None

    def grid(self, url: str) -> dict | None:
        from .weather import _cache_get, _cache_put
        ck = "nws_" + re.sub(r"[^A-Za-z0-9]", "_", url.split("/gridpoints/")[-1])
        cached = _cache_get(ck, self.ttl)
        if cached is not None:
            return cached
        data = self._get(url)
        props = (data or {}).get("properties")
        if props:
            _cache_put(ck, props)
        return props

    def fetch(self, keys: list[tuple[float, float]]) -> NwsResult:
        res = NwsResult()

        def one(key):
            url = self.grid_url(*key)
            if not url:
                return key, None
            return key, self.grid(url)

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(one, k) for k in keys]
            for f in futures:
                try:
                    key, props = f.result()
                except Exception as e:
                    res.failed += 1
                    res.errors.append(str(e)[:160])
                    continue
                if props:
                    res.series[key] = to_hourly(props)
                    res.ok_points += 1
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _POINTS_FILE.write_text(json.dumps(self.points))
        except OSError:
            pass
        return res


def to_hourly(props: dict, hours: int = 7 * 24 + 12) -> dict:
    """Expand an NWS gridpoint 'properties' object into hourly arrays."""
    t0 = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600 - 3 * 3600
    times = [t0 + i * 3600 for i in range(hours)]
    axis = {t: i for i, t in enumerate(times)}
    n = len(times)
    g = lambda name, conv=True: _layer_hourly(props.get(name), axis, n, conv)  # noqa: E731
    wx_raw = g("weather", conv=False)
    elev = props.get("elevation") or {}
    return {
        "elevation_m": _convert(elev.get("value"), elev.get("unitCode")) if isinstance(elev, dict) else None,
        "times": times,
        "vars": {
            "temperature_2m": g("temperature"),
            "dew_point_2m": g("dewpoint"),
            "sky_cover": g("skyCover"),
            "nws_ceiling_m": g("ceilingHeight"),
            "visibility": g("visibility"),
            "precipitation_probability": g("probabilityOfPrecipitation"),
            "thunder_prob": g("probabilityOfThunder"),
            "wind_speed_10m": g("windSpeed"),
            "wind_direction_10m": g("windDirection"),
            "wind_gusts_10m": g("windGust"),
            "transport_wind_kt": g("transportWindSpeed"),
            "transport_wind_dir": g("transportWindDirection"),
            "weather_code": [weather_code(v) if v is not None else None for v in wx_raw],
            "nws_has": [1 if v is not None else None for v in g("temperature")],
        },
    }
