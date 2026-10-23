"""Aircraft performance model built from POH-style YAML.

Numbers come from two places, merged in this order:

1. The type catalog, config/aircraft_types/<family>.yaml: one file per family
   (e.g. cessna-182) with shared `base` numbers and `variants` (182P, 182Q,
   182R, ...) that override only what differs. Type ids look like
   "cessna-182/182R".
2. "My airplane", config/aircraft.yaml (or a per-user dict): `type:` plus the
   owner's own numbers (tail number, confirmed fuel, empty weight, tune-ups),
   which win over the catalog.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from .config import CONFIG_DIR, load_yaml

TYPES_DIR = CONFIG_DIR / "aircraft_types"


def deep_merge(base: dict, over: dict) -> dict:
    """Copy of `base` with `over` merged in; nested dicts merge, everything else replaces."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass(frozen=True)
class AircraftType:
    id: str              # "cessna-182/182R"
    family: str          # "cessna-182"
    variant: str         # "182R"
    name: str            # "Cessna 182R Skylane"
    years: tuple[int, int]
    raw: dict            # base + variant, ready to fly

    @property
    def verified(self) -> bool:
        """True once the numbers have been checked against a real POH."""
        return bool(self.raw.get("verified"))


@lru_cache(maxsize=1)
def catalog() -> dict[str, AircraftType]:
    out: dict[str, AircraftType] = {}
    for path in sorted(TYPES_DIR.glob("*.yaml")):
        fam = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        family = fam["family"]
        for vid, var in (fam.get("variants") or {}).items():
            raw = deep_merge(fam.get("base") or {}, var)
            raw.setdefault("make", fam.get("make", ""))
            raw.setdefault("faa_mfr", fam.get("faa_mfr", ""))
            tid = f"{family}/{vid}"
            yrs = raw.get("years") or [0, 9999]
            out[tid] = AircraftType(tid, family, str(vid), raw.get("name", tid), (int(yrs[0]), int(yrs[1])), raw)
    return out


def get_type(type_id: str) -> AircraftType:
    try:
        return catalog()[type_id]
    except KeyError:
        raise KeyError(f"Unknown aircraft type {type_id!r}. Known: {', '.join(sorted(catalog()))}") from None


def resolve(profile: dict | None = None) -> dict:
    """Catalog numbers for profile['type'] with the owner's own numbers on top."""
    profile = load_yaml("aircraft.yaml") if profile is None else profile
    tid = profile.get("type")
    if not tid:
        return copy.deepcopy(profile)          # an old-style, fully spelled-out profile
    mine = {k: v for k, v in profile.items() if k != "type"}
    raw = deep_merge(get_type(tid).raw, mine)
    raw["type"] = tid
    fuel = raw.get("fuel", {})
    if "usable_gal" not in fuel:
        # No owner-confirmed fuel: use the type's smallest tank option until they say otherwise.
        opts = raw.get("fuel_options") or {}
        pick = mine.get("fuel_option") or min(opts, key=lambda k: opts[k]["usable_gal"])
        fuel.update(opts[pick])
        raw["fuel"] = fuel
    return raw


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


@dataclass
class ClimbResult:
    minutes: float
    gal: float
    nm: float


@dataclass
class Aircraft:
    raw: dict[str, Any]
    cruise_setting: str

    @classmethod
    def load(cls, cruise_setting: str | None = None, overrides: dict | None = None,
             profile: dict | None = None) -> "Aircraft":
        """`profile` is a "my airplane" dict (default: config/aircraft.yaml); `overrides` take dotted keys."""
        raw = resolve(profile)
        for k, v in (overrides or {}).items():
            # dotted keys like "fuel.usable_gal"
            node = raw
            parts = k.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = v
        setting = cruise_setting or raw.get("default_cruise_setting", "65%")
        if setting not in raw["cruise_settings"]:
            setting = raw.get("default_cruise_setting", next(iter(raw["cruise_settings"])))
        return cls(raw=raw, cruise_setting=setting)

    @classmethod
    def from_type(cls, type_id: str, cruise_setting: str | None = None, **mine) -> "Aircraft":
        """A catalog type with optional owner numbers, e.g. from_type("cessna-172/172N", tail_number="N12345")."""
        return cls.load(cruise_setting, profile={"type": type_id, **mine})

    # -- basic properties --------------------------------------------------
    @property
    def name(self) -> str:
        tail = self.raw.get("tail_number", "")
        tail = "" if tail.strip("N_ ") == "" else tail
        return f'{tail} {self.raw.get("model") or self.raw.get("name", "")}'.strip()

    @property
    def type_id(self) -> str:
        return self.raw.get("type", "")

    @property
    def verified(self) -> bool:
        """False while the performance numbers are typical values nobody has checked against a POH."""
        return bool(self.raw.get("verified", True))

    @property
    def has_weight_balance(self) -> bool:
        wb = self.raw.get("weight_balance") or {}
        return bool(wb.get("cg_envelope") and wb.get("empty_weight_lb"))

    @property
    def usable_gal(self) -> float:
        return float(self.raw["fuel"]["usable_gal"])

    @property
    def taxi_gal(self) -> float:
        return float(self.raw["fuel"].get("start_taxi_takeoff_gal", 2.0))

    @property
    def setting(self) -> dict:
        return self.raw["cruise_settings"][self.cruise_setting]

    @property
    def cruise_gph(self) -> float:
        return float(self.setting["gph"])

    @property
    def max_alt_ft(self) -> float:
        return min(float(self.setting.get("max_alt_ft", 12000)),
                   float(self.raw["limits"].get("max_planning_alt_ft", 10500)))

    @property
    def demo_xwind(self) -> float:
        return float(self.raw["limits"].get("max_demonstrated_crosswind_kt", 15))

    # -- performance -------------------------------------------------------
    def ktas(self, alt_ft: float) -> float:
        table = {int(k): float(v) for k, v in self.setting["ktas"].items()}
        xs = sorted(table)
        return _interp(alt_ft, xs, [table[x] for x in xs])

    def _climb_from_sl(self, alt_ft: float) -> ClimbResult:
        rows = sorted(self.raw["climb_table"], key=lambda r: r["alt_ft"])
        xs = [r["alt_ft"] for r in rows]
        return ClimbResult(
            minutes=_interp(alt_ft, xs, [r["minutes"] for r in rows]),
            gal=_interp(alt_ft, xs, [r["gal"] for r in rows]),
            nm=_interp(alt_ft, xs, [r["nm"] for r in rows]),
        )

    def climb(self, field_elev_ft: float, cruise_alt_ft: float) -> ClimbResult:
        """POH method: (table at cruise alt) minus (table at field elevation)."""
        top = self._climb_from_sl(cruise_alt_ft)
        bottom = self._climb_from_sl(min(field_elev_ft, cruise_alt_ft))
        return ClimbResult(max(0.0, top.minutes - bottom.minutes),
                           max(0.0, top.gal - bottom.gal),
                           max(0.0, top.nm - bottom.nm))

    def descent(self, cruise_alt_ft: float, field_elev_ft: float) -> ClimbResult:
        d = self.raw.get("descent", {})
        fpm = float(d.get("fpm", 500))
        gph = float(d.get("gph", 9.0))
        minutes = max(0.0, cruise_alt_ft - field_elev_ft) / fpm
        speed = self.ktas(cruise_alt_ft) + float(d.get("ktas_bonus", 5))
        return ClimbResult(minutes, gph * minutes / 60, speed * minutes / 60)

    def climb_speed_kt(self) -> float:
        # average TAS in a cruise climb (a 182 at Vy ~80 KIAS averages ~90 KTAS)
        return float(self.raw.get("climb_ktas", 90.0))

    def still_air_leg(self, dist_nm: float, dep_elev: float, arr_elev: float,
                      alt_ft: float = 6500, headwind_kt: float = 0) -> tuple[float, float]:
        """(hours, gallons excluding reserve) for a quick feasibility estimate."""
        c = self.climb(dep_elev, alt_ft)
        dsc = self.descent(alt_ft, arr_elev)
        cruise_nm = max(0.0, dist_nm - c.nm - dsc.nm)
        gs = max(40.0, self.ktas(alt_ft) - headwind_kt)
        cruise_h = cruise_nm / gs
        hours = (c.minutes + dsc.minutes) / 60 + cruise_h
        gal = self.taxi_gal + c.gal + dsc.gal + cruise_h * self.cruise_gph
        return hours, gal
