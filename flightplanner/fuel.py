"""Fuel availability at airports, from the FAA NASR extract in data/fuel.csv.

Built by scripts/build_fuel_data.py (refreshed weekly by GitHub Actions).
If the file isn't there yet, `avgas_status` returns None ("unknown") and the
planner falls back to its built-in list of airports known to sell 100LL.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache

from .config import DATA_DIR

FUEL_FILE = DATA_DIR / "fuel.csv"
# Fuel the O-470/IO-540-powered 182s can burn without an STC.
AVGAS = {"100LL", "100"}


@dataclass(frozen=True)
class FuelInfo:
    ident: str
    fuel_types: tuple[str, ...]
    attendance: str
    eff_date: str

    @property
    def has_avgas(self) -> bool:
        return bool(AVGAS & set(self.fuel_types))

    def summary(self) -> str:
        fuel = ", ".join(self.fuel_types) if self.fuel_types else "no fuel listed"
        att = f" · attended {self.attendance}" if self.attendance else ""
        return f"{fuel}{att}"


@lru_cache(maxsize=1)
def load_fuel() -> dict[str, FuelInfo]:
    out: dict[str, FuelInfo] = {}
    try:
        with open(FUEL_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                info = FuelInfo(r["ident"].upper(),
                                tuple(t for t in r["fuel_types"].split(",") if t),
                                r.get("attendance", ""), r.get("eff_date", ""))
                out[info.ident] = info
                faa = (r.get("faa_id") or "").upper()
                if faa and faa not in out:
                    out[faa] = info
    except FileNotFoundError:
        pass
    return out


def fuel_info(ident: str) -> FuelInfo | None:
    data = load_fuel()
    key = ident.upper()
    if key in data:
        return data[key]
    if len(key) == 4 and key.startswith("K") and key[1:] in data:
        return data[key[1:]]
    return None


def avgas_status(ident: str) -> bool | None:
    """True = sells 100LL/100, False = FAA data says it doesn't, None = unknown."""
    if not load_fuel():
        return None
    info = fuel_info(ident)
    return None if info is None else info.has_avgas


def price_url(ident: str) -> str:
    return f"https://www.airnav.com/airport/{ident.upper()}"
