"""Terrain along the route corridor (Open-Meteo elevation API, cached forever).

Used to set a minimum safe cruise altitude and to judge whether forecast
ceilings leave room to fly VFR over the Appalachians.
"""
from __future__ import annotations

import json

import requests

from .config import CACHE_DIR
from .geo import destination, initial_bearing, sample_line
from .weather import USER_AGENT

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
M_TO_FT = 3.28084
_CACHE_FILE = CACHE_DIR / "terrain_cache.json"

# Coarse, deliberately pessimistic fallback if the elevation service can't be
# reached. Boxes: (lat_lo, lat_hi, lon_lo, lon_hi, max_ft).
FALLBACK_BOXES = (
    (34.7, 37.2, -84.4, -80.9, 6700.0),   # Blue Ridge / Smokies (Mt. Mitchell 6,684)
    (36.0, 39.5, -82.5, -78.5, 5000.0),   # VA/WV ridges
    (34.5, 38.0, -85.8, -81.4, 4200.0),   # Cumberland Plateau, E. KY (Black Mtn 4,145)
)


def _key(lat: float, lon: float) -> str:
    return f"{lat:.3f},{lon:.3f}"


class Terrain:
    def __init__(self, session: requests.Session | None = None):
        self.sess = session or requests.Session()
        self.cache: dict[str, float] = {}
        self.used_fallback = False
        try:
            self.cache = json.loads(_CACHE_FILE.read_text())
        except Exception:
            self.cache = {}

    def _save(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(self.cache))
        except OSError:
            pass

    @staticmethod
    def _fallback(lat: float, lon: float) -> float:
        for lo, hi, wlo, whi, h in FALLBACK_BOXES:
            if lo <= lat <= hi and wlo <= lon <= whi:
                return h
        return 1500.0 if lat > 31.0 else 400.0

    def elevations(self, pts: list[tuple[float, float]]) -> list[float]:
        pts = [(round(a, 3), round(b, 3)) for a, b in pts]
        missing = sorted({p for p in pts if _key(*p) not in self.cache})
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            try:
                r = self.sess.get(ELEVATION_URL, timeout=30, headers={"User-Agent": USER_AGENT}, params={
                    "latitude": ",".join(f"{p[0]:.3f}" for p in chunk),
                    "longitude": ",".join(f"{p[1]:.3f}" for p in chunk)})
                r.raise_for_status()
                elev = r.json().get("elevation", [])
                for p, e in zip(chunk, elev):
                    if e is not None:
                        self.cache[_key(*p)] = float(e) * M_TO_FT
            except Exception:
                self.used_fallback = True
                break
        if missing:
            self._save()
        out = []
        for p in pts:
            v = self.cache.get(_key(*p))
            if v is None:
                self.used_fallback = True
                v = self._fallback(*p)
            out.append(v)
        return out

    def leg_profile(self, lat1: float, lon1: float, lat2: float, lon2: float,
                    halfwidth_nm: float, step_nm: float = 3.0) -> list[tuple[float, float]]:
        """[(dist_from_start_nm, max_terrain_ft_in_corridor)] every step_nm."""
        center = sample_line(lat1, lon1, lat2, lon2, step_nm)
        crs = initial_bearing(lat1, lon1, lat2, lon2)
        probe: list[tuple[float, float]] = []
        for la, lo, _ in center:
            probe.append((la, lo))
            for off in (-halfwidth_nm, -halfwidth_nm / 2, halfwidth_nm / 2, halfwidth_nm):
                probe.append(destination(la, lo, (crs + 90) % 360, off) if off > 0
                             else destination(la, lo, (crs - 90) % 360, -off))
        elev = self.elevations(probe)
        per = 5
        return [(center[i][2], max(elev[i * per:(i + 1) * per])) for i in range(len(center))]
