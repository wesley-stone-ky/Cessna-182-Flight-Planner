"""Terrain along the route corridor (AWS Terrain Tiles, cached forever).

Used to set a minimum safe cruise altitude and to judge whether forecast
ceilings leave room to fly VFR over the Appalachians.

Elevations come from the free "Terrain Tiles" open dataset on AWS (Terrarium
PNG tiles built from USGS/SRTM data; attribution in the README). At zoom 10 a
pixel is ~120 m in the US, and each point takes the highest of the 3x3 pixels
around it so a narrow ridge isn't missed.
"""
from __future__ import annotations

import io
import json
import math

import requests

from .config import CACHE_DIR
from .geo import destination, initial_bearing, sample_line
from .weather import USER_AGENT

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ZOOM = 10
M_TO_FT = 3.28084
# Tile pixels average the ground, which shaves summits (Mt. Mitchell reads 6,611 ft vs 6,684 ft
# charted), so every sample gets this pad on top.
PEAK_PAD_FT = 100.0
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
        self._tiles: dict[tuple[int, int], list | None] = {}
        self._offline = False
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

    def _tile(self, x: int, y: int):
        """Decoded elevation tile (meters, 256x256 rows), or None if unavailable."""
        k = (x, y)
        if k not in self._tiles:
            self._tiles[k] = None
            if not self._offline:
                try:
                    from PIL import Image
                    r = self.sess.get(TILE_URL.format(z=ZOOM, x=x, y=y), timeout=30,
                                      headers={"User-Agent": USER_AGENT})
                    r.raise_for_status()
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    raw, w = img.tobytes(), img.width
                    # Terrarium encoding: meters = R*256 + G + B/256 - 32768
                    self._tiles[k] = [[raw[i] * 256 + raw[i + 1] + raw[i + 2] / 256 - 32768
                                       for i in range(row * w * 3, (row + 1) * w * 3, 3)]
                                      for row in range(img.height)]
                except Exception:
                    self._offline = True   # don't wait on every tile once the service has failed
        return self._tiles[k]

    def _sample(self, lat: float, lon: float) -> float | None:
        """Highest elevation (ft) in the 3x3 pixels around a point."""
        n = 2 ** ZOOM
        fx = (lon + 180) / 360 * n
        lat_r = math.radians(lat)
        fy = (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * n
        tx, ty = int(fx), int(fy)
        px, py = int((fx - tx) * 256), int((fy - ty) * 256)
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                gx, gy = tx * 256 + px + dx, ty * 256 + py + dy
                tile = self._tile(gx // 256, gy // 256)
                if tile is None:
                    return None
                v = tile[gy % 256][gx % 256]
                best = v if best is None else max(best, v)
        return max(0.0, best) * M_TO_FT + PEAK_PAD_FT

    def elevations(self, pts: list[tuple[float, float]]) -> list[float]:
        pts = [(round(a, 3), round(b, 3)) for a, b in pts]
        missing = sorted({p for p in pts if _key(*p) not in self.cache})
        for p in missing:
            v = self._sample(*p)
            if v is not None:
                self.cache[_key(*p)] = v
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
