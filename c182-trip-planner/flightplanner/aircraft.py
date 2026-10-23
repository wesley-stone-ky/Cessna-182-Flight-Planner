"""Aircraft performance model built from the POH-style YAML profile."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import load_yaml


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
    def load(cls, cruise_setting: str | None = None, overrides: dict | None = None) -> "Aircraft":
        raw = load_yaml("aircraft.yaml")
        for k, v in (overrides or {}).items():
            # dotted keys like "fuel.usable_gal"
            node = raw
            parts = k.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = v
        setting = cruise_setting or raw.get("default_cruise_setting", "65%")
        return cls(raw=raw, cruise_setting=setting)

    # -- basic properties --------------------------------------------------
    @property
    def name(self) -> str:
        return f'{self.raw.get("tail_number", "")} {self.raw.get("model", "")}'.strip()

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
        # rough average TAS in a cruise climb for a 182 (Vy ~ 80 KIAS -> ~90 KTAS avg)
        return 90.0

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
