"""Load the aircraft profile and personal minimums from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"


@dataclass
class Minimums:
    flight_rules: str = "VFR"
    airport_min_ceiling_ft_agl: float = 3000
    airport_min_vis_sm: float = 5
    max_crosswind_kt: float = 12
    max_gust_kt: float = 25
    max_surface_wind_kt: float = 25
    max_gust_spread_kt: float = 15
    wind_window_minutes: float = 60
    enroute_min_vis_sm: float = 5
    terrain_clearance_ft: float = 1000
    mountain_terrain_clearance_ft: float = 2000
    cloud_clearance_below_ft: float = 500
    corridor_halfwidth_nm: float = 5
    cape_caution_jkg: float = 500
    cape_nogo_jkg: float = 1500
    precip_prob_caution_pct: float = 50
    mountain_wind_caution_kt: float = 35
    reserve_minutes: float = 60
    max_leg_hours: float = 3.5
    planning_headwind_kt: float = 10
    fuel_stop_ground_minutes: float = 45
    day_only: bool = True
    sunset_buffer_minutes: float = 30
    night_current: bool = False
    min_runway_ft: float = 3000
    prefer_known_fuel_stops: bool = True

    @property
    def ifr(self) -> bool:
        return str(self.flight_rules).upper() == "IFR"


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_minimums(overrides: dict | None = None) -> Minimums:
    raw = load_yaml("minimums.yaml")
    raw.update(overrides or {})
    known = {f.name for f in fields(Minimums)}
    return Minimums(**{k: v for k, v in raw.items() if k in known})


def minimum_presets() -> dict[str, dict]:
    """{"cautious": {"label", "blurb", <Minimums fields>...}, ...} from config/minimum_presets.yaml."""
    return load_yaml("minimum_presets.yaml")


def preset_minimums(name: str, overrides: dict | None = None) -> Minimums:
    """Minimums for an onboarding preset, with any fine-tuned numbers on top."""
    p = {k: v for k, v in minimum_presets()[name].items() if k not in ("label", "blurb")}
    p.update(overrides or {})
    return load_minimums(p)
