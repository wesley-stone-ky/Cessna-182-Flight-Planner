"""Great-circle math, wind triangle, magnetic variation, polygons."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from functools import lru_cache

EARTH_NM = 3440.065


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_NM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True course in degrees 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def intermediate(lat1: float, lon1: float, lat2: float, lon2: float, frac: float) -> tuple[float, float]:
    """Point at fraction `frac` along the great circle."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = haversine_nm(lat1, lon1, lat2, lon2) / EARTH_NM
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - frac) * d) / math.sin(d)
    b = math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def destination(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    d = dist_nm / EARTH_NM
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def sample_line(lat1: float, lon1: float, lat2: float, lon2: float, step_nm: float) -> list[tuple[float, float, float]]:
    """Points every ~step_nm along the leg, inclusive of both ends: (lat, lon, dist_from_start)."""
    total = haversine_nm(lat1, lon1, lat2, lon2)
    n = max(1, math.ceil(total / step_nm))
    out = []
    for i in range(n + 1):
        f = i / n
        la, lo = intermediate(lat1, lon1, lat2, lon2, f)
        out.append((la, lo, total * f))
    return out


def cross_along_track(lat1, lon1, lat2, lon2, plat, plon) -> tuple[float, float]:
    """(cross-track nm [+right of course], along-track nm) of point P vs leg 1->2."""
    d13 = haversine_nm(lat1, lon1, plat, plon) / EARTH_NM
    t13 = math.radians(initial_bearing(lat1, lon1, plat, plon))
    t12 = math.radians(initial_bearing(lat1, lon1, lat2, lon2))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    cos_ratio = math.cos(d13) / max(1e-12, math.cos(xt))
    at = math.acos(max(-1.0, min(1.0, cos_ratio)))
    if math.cos(t13 - t12) < 0:
        at = -at
    return xt * EARTH_NM, at * EARTH_NM


def wind_triangle(true_course: float, tas_kt: float, wind_from_deg: float, wind_kt: float) -> tuple[float, float]:
    """Return (groundspeed_kt, wind_correction_angle_deg). Wind direction is FROM, true."""
    if tas_kt <= 0:
        return 0.0, 0.0
    awa = math.radians(wind_from_deg - true_course)
    ratio = max(-1.0, min(1.0, wind_kt * math.sin(awa) / tas_kt))
    wca = math.asin(ratio)
    gs = tas_kt * math.cos(wca) - wind_kt * math.cos(awa)
    return max(gs, 20.0), math.degrees(wca)


def wind_components(runway_heading_true: float, wind_from_deg: float, wind_kt: float) -> tuple[float, float]:
    """(headwind, crosswind) magnitude for a runway heading. Headwind negative = tailwind."""
    a = math.radians(wind_from_deg - runway_heading_true)
    return wind_kt * math.cos(a), abs(wind_kt * math.sin(a))


@lru_cache(maxsize=4096)
def _magvar_cached(lat_r: float, lon_r: float, year_r: float) -> float:
    try:
        from pygeomag import GeoMag
        return float(GeoMag().calculate(glat=lat_r, glon=lon_r, alt=0, time=year_r,
                                        allow_date_outside_lifespan=True).d)
    except Exception:
        # Southeast US fallback: roughly 4-9 degrees west. Linear in longitude.
        return -(5.0 + (lon_r + 85.0) * 0.6)


def magnetic_variation(lat: float, lon: float, when: datetime | None = None) -> float:
    """Declination in degrees, east positive (west negative, as in the SE US)."""
    when = when or datetime.now(timezone.utc)
    year = when.year + (when.timetuple().tm_yday / 365.25)
    return _magvar_cached(round(lat, 1), round(lon, 1), round(year, 1))


def true_to_magnetic(true_deg: float, lat: float, lon: float, when: datetime | None = None) -> float:
    return (true_deg - magnetic_variation(lat, lon, when)) % 360


def point_in_polygon(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    """Ray casting; poly is [(lat, lon), ...]."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside
