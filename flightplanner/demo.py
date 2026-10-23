"""Synthetic forecast for demos and tests (no network needed).

Builds Open-Meteo-shaped data: mostly good weather, with a cold front that
crosses the route on day 3 (low ceilings, rain, thunderstorms in the south)
and a gusty, post-frontal day 4.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .weather import LEVELS_HPA, GridForecast, snap


def synthetic_forecast(points, start: datetime | None = None, days: int = 10, seed: int = 0) -> GridForecast:
    start = (start or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    hours = days * 24
    times = [int((start + timedelta(hours=h)).timestamp()) for h in range(hours)]
    series = {}
    for key in sorted({snap(la, lo) for la, lo in points}):
        lat, lon = key
        mountains = 34.5 <= lat <= 37.5 and -84.8 <= lon <= -81.0
        elev_m = 900.0 if mountains else (250.0 if lat > 33 else 20.0)
        h = {k: [] for k in ("temperature_2m", "dew_point_2m", "cloud_cover_low", "cloud_cover_mid",
                             "visibility", "precipitation_probability", "precipitation", "weather_code",
                             "cape", "freezing_level_height", "wind_speed_10m", "wind_direction_10m",
                             "wind_gusts_10m")}
        for p in LEVELS_HPA:
            for v in ("wind_speed", "wind_direction", "temperature", "relative_humidity", "geopotential_height"):
                h[f"{v}_{p}hPa"] = []
        for i, ts in enumerate(times):
            t = datetime.fromtimestamp(ts, tz=timezone.utc)
            day = i / 24.0
            local_hr = (t.hour - 4) % 24
            diurnal = math.sin((local_hr - 9) / 24 * 2 * math.pi)
            # front sweeps NW->SE through day 2.5..3.5 (later further south)
            front_center = 2.6 + (38.5 - lat) * 0.08
            f = math.exp(-((day - front_center) / 0.35) ** 2)
            post = 1.0 if front_center + 0.4 < day < front_center + 1.4 else 0.0
            temp = 24 - (lat - 28) * 1.1 + 6 * diurnal - 8 * post - (elev_m / 300)
            dew = temp - (8 + 4 * diurnal) * (1 - f) - 1 * f
            low = 15 + 80 * f
            mid = 20 + 60 * f
            pop = 5 + 85 * f
            code = 0 if f < 0.2 else (63 if f < 0.7 else (95 if lat < 34 else 63))
            cape = 100 + 1800 * f * (1 if lat < 34 else 0.3)
            vis = 30000 if f < 0.4 else (6000 if f < 0.75 else 3000)
            wsfc = 6 + 6 * f + 12 * post
            gust = wsfc + 6 + 10 * post
            wdir_sfc = 200 if day < front_center else 320
            h["temperature_2m"].append(round(temp, 1))
            h["dew_point_2m"].append(round(dew, 1))
            h["cloud_cover_low"].append(round(low))
            h["cloud_cover_mid"].append(round(mid))
            h["visibility"].append(vis)
            h["precipitation_probability"].append(round(pop))
            h["precipitation"].append(round(3 * f, 1))
            h["weather_code"].append(code)
            h["cape"].append(round(cape))
            h["freezing_level_height"].append(3800 - 1200 * post)
            h["wind_speed_10m"].append(round(wsfc, 1))
            h["wind_direction_10m"].append(wdir_sfc)
            h["wind_gusts_10m"].append(round(gust, 1))
            for p, z, base_spd in ((925, 760, 15), (850, 1500, 20), (700, 3100, 28), (600, 4300, 35)):
                if p not in LEVELS_HPA:
                    continue
                h[f"wind_speed_{p}hPa"].append(round(base_spd + 10 * f + 15 * post, 1))
                h[f"wind_direction_{p}hPa"].append(250 if day < front_center else 310)
                h[f"temperature_{p}hPa"].append(round(temp - (z - elev_m) * 0.0065, 1))
                h[f"relative_humidity_{p}hPa"].append(round(45 + 50 * f))
                h[f"geopotential_height_{p}hPa"].append(z)
        h["time"] = times
        series[key] = {"latitude": lat, "longitude": lon, "elevation": elev_m, "hourly": h}
    return GridForecast(series, fetched_at=start)
