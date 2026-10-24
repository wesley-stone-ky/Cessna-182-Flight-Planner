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
    self_serve: bool = False

    @property
    def has_avgas(self) -> bool:
        return bool(AVGAS & set(self.fuel_types))

    def summary(self) -> str:
        fuel = ", ".join(self.fuel_types) if self.fuel_types else "no fuel listed"
        att = f" · attended {self.attendance}" if self.attendance else ""
        ss = " · self-serve" if self.self_serve else ""
        return f"{fuel}{att}{ss}"


@lru_cache(maxsize=1)
def load_fuel() -> dict[str, FuelInfo]:
    out: dict[str, FuelInfo] = {}
    try:
        with open(FUEL_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                info = FuelInfo(r["ident"].upper(),
                                tuple(t for t in r["fuel_types"].split(",") if t),
                                r.get("attendance", ""), r.get("eff_date", ""),
                                (r.get("self_serve") or "") == "1")
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


# ---------------------------------------------------------------------------
# Attendance (FAA APT_ATT): "ALL ALL 0800-1700; ALL SAT-SUN 0900-1600", "UNATNDD", ...
# ---------------------------------------------------------------------------
_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UNATTENDED_WORDS = ("UNATNDD", "UNATTENDED", "UNATTND", "UNATND")


def _expand(spec: str, names: list[str]) -> set[int] | None:
    spec = spec.upper()
    if spec == "ALL":
        return set(range(len(names)))
    if spec in ("WKDAYS", "WKDS", "WEEKDAYS"):
        spec = "MON-FRI"
    if spec in ("WKENDS", "WEEKENDS"):
        spec = "SAT-SUN"
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a[:3] not in names or b[:3] not in names:
                return None
            i, j = names.index(a[:3]), names.index(b[:3])
            k = i
            while True:
                out.add(k)
                if k == j:
                    break
                k = (k + 1) % len(names)
        elif part[:3] in names:
            out.add(names.index(part[:3]))
        else:
            return None
    return out


def _minutes(tok: str, dawn: int, dusk: int) -> int | None:
    tok = tok.upper()
    if tok in ("DAWN", "SR"):
        return dawn
    if tok in ("DUSK", "SS"):
        return dusk
    if len(tok) == 4 and tok.isdigit():
        h, m = int(tok[:2]), int(tok[2:])
        return h * 60 + m if h <= 24 and m < 60 else None
    return None


def is_unattended(attendance: str) -> bool:
    return any(w in attendance.upper() for w in UNATTENDED_WORDS)


def attended_at(attendance: str, local_dt, sunrise_local=None, sunset_local=None) -> bool | None:
    """Is the FBO attended at `local_dt` (airport local time)?

    True / False, or None when the schedule can't be read (IREG, ON CALL, remarks...)."""
    text = (attendance or "").strip().upper()
    if not text:
        return None
    if is_unattended(text):
        return False
    dawn = sunrise_local.hour * 60 + sunrise_local.minute if sunrise_local else 6 * 60
    dusk = sunset_local.hour * 60 + sunset_local.minute if sunset_local else 19 * 60
    now_min = local_dt.hour * 60 + local_dt.minute
    understood = False
    for seg in text.split(";"):
        toks = seg.split()
        if len(toks) != 3:
            continue
        months, days, hours = toks
        mset, dset = _expand(months, _MONTHS), _expand(days, _DAYS)
        if mset is None or dset is None:
            continue
        if hours in ("ALL", "24HRS", "0000-2400", "0000-2359"):
            span = (0, 24 * 60)
        elif hours == "DALGT":
            span = (dawn, dusk)
        elif "-" in hours:
            a, b = hours.split("-", 1)
            sa, sb = _minutes(a, dawn, dusk), _minutes(b, dawn, dusk)
            if sa is None or sb is None:
                continue
            span = (sa, sb)
        else:
            continue
        understood = True
        if (local_dt.month - 1) in mset and local_dt.weekday() in dset:
            lo, hi = span
            if (lo <= now_min < hi) if lo <= hi else (now_min >= lo or now_min < hi):
                return True
    return False if understood else None
