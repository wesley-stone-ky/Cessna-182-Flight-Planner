"""Turn a stored pilot into the engine's Minimums and Aircraft."""
from __future__ import annotations

from dataclasses import replace

from flightplanner.aircraft import Aircraft, catalog
from flightplanner.config import Minimums, preset_minimums

from .db import User

# Fine-tune limits: never below the legal VFR floors (14 CFR 91.155, 91.151).
TUNABLE = {
    "airport_min_ceiling_ft_agl": ("Lowest ceiling at the airport", "ft", 1000, 6000, 100),
    "airport_min_vis_sm": ("Lowest visibility", "miles", 3, 10, 1),
    "max_crosswind_kt": ("Most crosswind (including gusts)", "kt", 5, 20, 1),
    "max_surface_wind_kt": ("Most wind", "kt", 10, 35, 1),
    "max_gust_kt": ("Most gusts", "kt", 10, 35, 1),
}


def clamp_minimums(raw: dict) -> dict:
    out = {}
    for k, (_, _, lo, hi, _) in TUNABLE.items():
        if k in raw and raw[k] not in (None, ""):
            out[k] = min(hi, max(lo, float(raw[k])))
    return out


def user_aircraft(u: User) -> Aircraft | None:
    t = (u.aircraft or {}).get("type")
    if not t or t not in catalog():
        return None
    return Aircraft.load(profile=u.aircraft)


def user_minimums(u: User) -> Minimums:
    m = preset_minimums(u.preset, clamp_minimums(u.minimums))
    ac = user_aircraft(u)
    if ac is not None and ac.demo_xwind < m.max_crosswind_kt:
        m = replace(m, max_crosswind_kt=ac.demo_xwind)   # never more than the airplane was demonstrated in
    return m
