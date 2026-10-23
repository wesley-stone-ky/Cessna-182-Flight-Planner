"""Tail-number lookup in the FAA aircraft registry, and matching to the type catalog.

Data: data/faa_aircraft.csv.gz + faa_models.csv + faa_engines.csv, built by
scripts/build_faa_registry.py from the FAA Releasable Aircraft Database
(public). Only valid registrations of piston airplanes, and no owner data.
"""
from __future__ import annotations

import csv
import gzip
import re
from dataclasses import dataclass
from functools import lru_cache

from .aircraft import catalog
from .config import DATA_DIR

_SUFFIXES = re.compile(r"\b(AIRCRAFT|AVIATION|CORPORATION|CORP|CO|INC|LLC|DESIGN|COMPANY|DIV|DIVISION)\b\.?")


@dataclass(frozen=True)
class Registration:
    n_number: str        # "N732RX"
    year: int | None
    make: str            # "Cessna"
    model: str           # "182R"
    engine: str          # "Continental O-470-U, 230 hp"
    seats: int | None
    hp: int | None
    multi_engine: bool
    amateur_built: bool
    type_id: str | None  # catalog match, e.g. "cessna-182/182R"

    def engine_note(self) -> str | None:
        """A warning when the registered engine isn't the one the catalog numbers assume (STCs are common)."""
        if not self.type_id or not self.hp:
            return None
        t = catalog()[self.type_id]
        book = {int(x) for x in re.findall(r"(\d{2,3}) hp", t.raw.get("engine", ""))}
        if book and self.hp not in book:
            stock = " or ".join(f"{h} hp" for h in sorted(book))
            return (f"Your registration lists a {self.hp} hp engine, but the {t.name} numbers are for the stock "
                    f"{stock}. Enter the cruise speed and fuel burn you actually see.")
        return None

    @property
    def description(self) -> str:
        yr = f"{self.year} " if self.year else ""
        return f"{yr}{self.make} {self.model}" + (f" with a {self.engine}" if self.engine else "")


def normalize_n(n: str) -> str:
    """'n732rx', 'N 732RX', '732RX' -> '732RX' (the registry stores it without the N)."""
    s = re.sub(r"[^A-Za-z0-9]", "", n or "").upper()
    return s[1:] if s.startswith("N") else s


_ENGINE_MAKERS = {"CONT MOTOR": "Continental", "CONTINENTAL": "Continental", "LYCOMING": "Lycoming",
                  "TEXTRON LYCOMING": "Lycoming", "ROTAX": "Rotax", "FRANKLIN": "Franklin"}


def _pretty_make(mfr: str) -> str:
    if mfr.startswith("TEXTRON AVIATION"):
        return "Cessna (Textron)"
    for k, v in _ENGINE_MAKERS.items():
        if mfr.startswith(k):
            return v
    s = _SUFFIXES.sub("", mfr).strip(" .,")
    return " ".join(w if len(w) <= 2 and not w.isalpha() else w.title() for w in s.split())


@lru_cache(maxsize=1)
def _models() -> dict[str, tuple]:
    with open(DATA_DIR / "faa_models.csv", newline="", encoding="utf-8") as f:
        return {r["code"]: (r["mfr"], r["model"], r["type_acft"], r["build_cert"], r["seats"])
                for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def _engines() -> dict[str, tuple]:
    with open(DATA_DIR / "faa_engines.csv", newline="", encoding="utf-8") as f:
        return {r["code"]: (r["mfr"], r["model"], r["hp"]) for r in csv.DictReader(f)}


def match_type(mfr: str, model: str, year: int | None = None) -> str | None:
    """Catalog type id for an FAA make/model string, or None if it isn't in the catalog yet."""
    hits = [t for t in catalog().values()
            if model in (t.raw.get("faa_models") or []) and re.search(t.raw.get("faa_mfr") or "^$", mfr)]
    if not hits:
        return None
    if year:
        for t in hits:
            if t.years[0] <= year <= t.years[1]:
                return t.id
    return hits[0].id


@lru_cache(maxsize=256)
def lookup(n_number: str) -> Registration | None:
    key = normalize_n(n_number)
    if not key:
        return None
    with gzip.open(DATA_DIR / "faa_aircraft.csv.gz", "rt", encoding="utf-8", newline="") as f:
        for r in csv.reader(f):
            if r[0] == key:
                break
        else:
            return None
    m = _models().get(r[2])
    if not m:
        return None
    mfr, model, type_acft, build_cert, seats = m
    e = _engines().get(r[3])
    engine, hp = "", 0
    if e and e[0] not in ("NONE", ""):
        hp = int(e[2]) if e[2].isdigit() else 0
        engine = f"{_pretty_make(e[0])} {e[1]}" + (f", {hp} hp" if hp else "")
    year = int(r[1]) if r[1].isdigit() else None
    return Registration("N" + key, year, _pretty_make(mfr), model, engine,
                        int(seats) if seats.isdigit() else None, hp or None, type_acft == "5", build_cert == "1",
                        match_type(mfr, model, year))
