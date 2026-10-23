"""Weight & balance for the C182 (limits and arms from config/aircraft.yaml).

Checks takeoff and landing weight against the limits, the CG against the
envelope at both points, baggage compartment limits, and works out how much
fuel you can carry with a given load. The planner uses that fuel (not
necessarily full tanks) for its range and reserve math.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import load_yaml


@dataclass
class Loading:
    front_lb: float = 170.0          # pilot + front passenger
    rear_lb: float = 0.0
    bag_a_lb: float = 0.0
    bag_b_lb: float = 0.0
    fuel_gal: float | None = None    # None = as much as fits (up to usable)


@dataclass
class WBPoint:
    label: str
    weight_lb: float
    moment: float

    @property
    def cg_in(self) -> float:
        return self.moment / self.weight_lb if self.weight_lb else 0.0


@dataclass
class WBResult:
    fuel_gal: float
    points: list[WBPoint]
    problems: list[tuple[str, str]] = field(default_factory=list)   # (level, text)
    max_fuel_gal: float = 0.0
    example_empty: bool = False

    @property
    def ok(self) -> bool:
        return not any(level == "NO-GO" for level, _ in self.problems)


def _interp(w: float, pts: list[list[float]]) -> float:
    pts = sorted(pts)
    if w <= pts[0][0]:
        return pts[0][1]
    if w >= pts[-1][0]:
        return pts[-1][1]
    for (w0, a0), (w1, a1) in zip(pts, pts[1:]):
        if w <= w1:
            return a0 + (a1 - a0) * (w - w0) / (w1 - w0)
    return pts[-1][1]


class WeightBalance:
    def __init__(self, raw: dict | None = None, usable_gal: float | None = None):
        cfg = load_yaml("aircraft.yaml") if raw is None else raw
        self.c = cfg["weight_balance"]
        self.usable_gal = float(usable_gal if usable_gal is not None else cfg["fuel"]["usable_gal"])

    # -- limits ---------------------------------------------------------------
    def fwd_limit(self, w: float) -> float:
        return _interp(w, self.c["cg_envelope"]["forward"])

    def aft_limit(self, w: float) -> float:
        return _interp(w, self.c["cg_envelope"]["aft"])

    def in_envelope(self, p: WBPoint) -> bool:
        return self.fwd_limit(p.weight_lb) - 1e-6 <= p.cg_in <= self.aft_limit(p.weight_lb) + 1e-6

    # -- loading --------------------------------------------------------------
    def zero_fuel(self, ld: Loading) -> WBPoint:
        s = self.c["stations"]
        items = [
            (float(self.c["empty_weight_lb"]), float(self.c["empty_arm_in"])),
            (ld.front_lb, s["front_seats"]["arm_in"]),
            (ld.rear_lb, s["rear_seats"]["arm_in"]),
            (ld.bag_a_lb, s["baggage_a"]["arm_in"]),
            (ld.bag_b_lb, s["baggage_b"]["arm_in"]),
        ]
        w = sum(i[0] for i in items)
        return WBPoint("Zero fuel", w, sum(i[0] * i[1] for i in items))

    def with_fuel(self, zf: WBPoint, gal: float, label: str) -> WBPoint:
        lb = gal * float(self.c["fuel_lb_per_gal"])
        return WBPoint(label, zf.weight_lb + lb, zf.moment + lb * float(self.c["fuel_arm_in"]))

    def max_fuel(self, ld: Loading) -> float:
        room = float(self.c["max_takeoff_lb"]) - self.zero_fuel(ld).weight_lb
        return max(0.0, min(self.usable_gal, room / float(self.c["fuel_lb_per_gal"])))

    def evaluate(self, ld: Loading, burn_gal: float = 0.0) -> WBResult:
        zf = self.zero_fuel(ld)
        mx = self.max_fuel(ld)
        fuel = mx if ld.fuel_gal is None else min(float(ld.fuel_gal), self.usable_gal)
        to = self.with_fuel(zf, fuel, "Takeoff")
        land = self.with_fuel(zf, max(0.0, fuel - burn_gal), "Landing")
        res = WBResult(fuel, [to, land, zf], max_fuel_gal=mx, example_empty=bool(self.c.get("empty_is_example")))
        p = res.problems
        s = self.c["stations"]
        if ld.bag_a_lb > s["baggage_a"]["max_lb"]:
            p.append(("NO-GO", f"Baggage area A {ld.bag_a_lb:.0f} lb is over its {s['baggage_a']['max_lb']} lb limit"))
        if ld.bag_b_lb > s["baggage_b"]["max_lb"]:
            p.append(("NO-GO", f"Baggage area B {ld.bag_b_lb:.0f} lb is over its {s['baggage_b']['max_lb']} lb limit"))
        if ld.bag_a_lb + ld.bag_b_lb > self.c["baggage_total_max_lb"]:
            p.append(("NO-GO", f"Total baggage {ld.bag_a_lb + ld.bag_b_lb:.0f} lb is over the "
                               f"{self.c['baggage_total_max_lb']} lb limit"))
        if to.weight_lb > self.c["max_takeoff_lb"] + 0.5:
            p.append(("NO-GO", f"Takeoff weight {to.weight_lb:,.0f} lb is over the {self.c['max_takeoff_lb']:,} lb "
                               f"maximum — carry no more than {mx:.0f} gal with this load"))
        if land.weight_lb > self.c["max_landing_lb"] + 0.5:
            p.append(("NO-GO", f"Landing weight {land.weight_lb:,.0f} lb is over the {self.c['max_landing_lb']:,} lb "
                               "maximum landing weight"))
        for pt in (to, land, zf):
            if not self.in_envelope(pt):
                side = "forward of" if pt.cg_in < self.fwd_limit(pt.weight_lb) else "aft of"
                lim = self.fwd_limit(pt.weight_lb) if side == "forward of" else self.aft_limit(pt.weight_lb)
                p.append(("NO-GO", f"{pt.label} CG {pt.cg_in:.1f} in is {side} the {lim:.1f} in limit "
                                   f"at {pt.weight_lb:,.0f} lb — move weight {'aft' if side == 'forward of' else 'forward'}"))
        if fuel < self.usable_gal - 0.5 and ld.fuel_gal is None:
            p.append(("INFO", f"With this load you can carry {mx:.0f} of {self.usable_gal:.0f} gal before reaching "
                              f"{self.c['max_takeoff_lb']:,} lb"))
        return res

    def envelope_polygon(self) -> list[tuple[float, float]]:
        """(cg_in, weight_lb) outline for charts."""
        f = sorted(self.c["cg_envelope"]["forward"])
        a = sorted(self.c["cg_envelope"]["aft"])
        lo_w = min(f[0][0], a[0][0]) - 600
        pts = [(f[0][1], lo_w)] + [(arm, w) for w, arm in f] + [(arm, w) for w, arm in reversed(a)] + [(a[0][1], lo_w)]
        return pts
