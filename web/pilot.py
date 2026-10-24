"""Turn a stored pilot into the engine's Minimums and Aircraft."""
from __future__ import annotations

from dataclasses import replace

from flightplanner.aircraft import Aircraft, catalog, deep_merge
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


def tuned_profile(profile: dict, usable_gal=None, ktas_6000=None, gph=None) -> dict:
    """The airplane profile with the owner's usable fuel, cruise speed and fuel burn (at the default power).

    Speed scales the whole cruise table so the shape with altitude stays the book's; blank means "keep the
    type's typical number". Values are kept inside sane bounds.
    """
    out = deep_merge(profile, {})
    if not out.get("type"):
        return out
    base = Aircraft.load(profile={k: v for k, v in out.items() if k not in ("cruise_settings",)})
    setting = base.cruise_setting
    if usable_gal not in (None, ""):
        tanks = [o.get("total_gal", 0) for o in (base.raw.get("fuel_options") or {}).values()]
        biggest = max(tanks + [base.raw["fuel"].get("total_gal", 0)]) or 150.0   # long-range tanks are an option
        u = min(max(float(usable_gal), 10.0), float(biggest))
        out.setdefault("fuel", {})["usable_gal"] = u
    tune = {}
    if gph not in (None, ""):
        g = min(max(float(gph), 3.0), 30.0)
        if abs(g - base.cruise_gph) > 0.01:
            tune["gph"] = g
    if ktas_6000 not in (None, ""):
        k = min(max(float(ktas_6000), 60.0), 250.0)
        if abs(k - base.ktas(6000)) > 0.5:
            tune["ktas"] = {a: round(v * k / base.ktas(6000), 1) for a, v in base.setting["ktas"].items()}
    out.pop("cruise_settings", None)
    if tune:
        out["cruise_settings"] = {setting: tune}
    return out
