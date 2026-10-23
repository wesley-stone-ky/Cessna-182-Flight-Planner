"""Airport database (bundled OurAirports extract) and fuel-stop search."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache

from .config import DATA_DIR
from .geo import cross_along_track, haversine_nm

# Airports along the Ohio Valley -> Southeast corridors that are widely known to
# sell 100LL. Fuel availability and hours change: ALWAYS confirm on AirNav or
# the Chart Supplement before counting on it.
KNOWN_FUEL = {
    # Kentucky / Tennessee
    "KLEX", "KLOZ", "KSME", "K1A6", "KCSV", "KTYS", "KDKX", "KMMI", "KCHA", "KMQY",
    "KTRI", "KMOR", "KGKT", "KRZR", "KBNA", "KSYI", "KBWG", "KEKX", "KSDF", "KLOU",
    # Georgia
    "KRMG", "KCZL", "KVPC", "KPDK", "KFTY", "KLZU", "KGVL", "KAHN", "KMCN", "KMLJ",
    "KCSG", "KABY", "KVLD", "KVDI", "KSAV", "KBQK", "KTMA", "KAMG", "KCKF", "KDNN",
    "KLGC", "KFFC", "KWDR", "KTOC", "KAGS", "KDQH", "KMGR",
    # Carolinas
    "KAVL", "KGSP", "KGMU", "KGYH", "KAND", "KCEU", "KHKY", "KJQF", "KEQY", "KUZA",
    "KCAE", "KCUB", "KOGB", "KFLO", "KMYR", "KCRE", "KHYW", "KGGE", "KCHS", "KJZI",
    "KLRO", "KMKS", "KCDN", "KSMS", "KAIK", "KDCM", "KCLT", "KEXX", "KINT", "KGSO",
    "KTDF", "KMEB", "KLBT", "KILM",
    # Florida
    "KTLH", "KGNV", "KOCF", "KCGC", "KBKV", "KLAL", "KPIE", "KSPG", "KTPF", "KVDF",
    "KZPH", "KLEE", "KX60", "K42J", "KCTY", "K40J", "KVQQ", "KCRG", "KJAX", "KFHB",
    "KSGJ", "KDED", "KDAB", "KORL", "KISM", "KSFB", "KBOW", "KGIF", "KSRQ", "KX39",
    # Alabama
    "KHSV", "KDCU", "KGAD", "KANB", "KBHM", "KMGM", "KDHN", "KTOI", "KEUF",
}

FAVORITES = {
    "KLEX": "Lexington Blue Grass (home)",
    "KGGE": "Georgetown County, SC",
    "KSPG": "Albert Whitted, St. Pete (by the pier)",
    "KTPF": "Peter O. Knight, Tampa (Davis Islands)",
}


@dataclass(frozen=True)
class Runway:
    ident: str
    heading_true: float
    length_ft: int
    surface: str
    lighted: bool


@dataclass(frozen=True)
class Airport:
    ident: str
    name: str
    type: str
    lat: float
    lon: float
    elev_ft: float
    region: str
    city: str
    longest_rwy_ft: int
    lighted: bool
    runways: tuple[Runway, ...] = field(default_factory=tuple)

    @property
    def known_fuel(self) -> bool:
        return self.ident in KNOWN_FUEL

    @property
    def label(self) -> str:
        return f"{self.ident} – {self.name} ({self.city})"

    @property
    def state(self) -> str:
        return self.region.replace("US-", "")


class AirportDB:
    def __init__(self, airports: dict[str, Airport]):
        self.airports = airports

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "AirportDB":
        rws: dict[str, list[Runway]] = {}
        with open(DATA_DIR / "runways.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rws.setdefault(r["ident"], []).append(Runway(
                    r["rwy"], float(r["heading_true"]), int(r["length_ft"]),
                    r["surface"], r["lighted"] == "1"))
        out: dict[str, Airport] = {}
        with open(DATA_DIR / "airports.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ident = r["ident"].upper()
                out[ident] = Airport(
                    ident=ident, name=r["name"], type=r["type"],
                    lat=float(r["lat"]), lon=float(r["lon"]),
                    elev_ft=float(r["elev_ft"] or 0), region=r["region"], city=r["city"],
                    longest_rwy_ft=int(r["longest_rwy_ft"]), lighted=r["lighted"] == "1",
                    runways=tuple(rws.get(ident, ())))
        return cls(out)

    def get(self, ident: str) -> Airport:
        key = ident.strip().upper()
        if key in self.airports:
            return self.airports[key]
        if len(key) == 3 and "K" + key in self.airports:
            return self.airports["K" + key]
        raise KeyError(f"Airport {ident!r} not found in the bundled database "
                       "(public airports with a paved runway >= 2,500 ft in the Southeast).")

    def search(self, text: str, limit: int = 15) -> list[Airport]:
        t = text.strip().lower()
        if not t:
            return []
        hits = [a for a in self.airports.values()
                if t in a.ident.lower() or t in a.name.lower() or t in a.city.lower()]
        hits.sort(key=lambda a: (not a.ident.lower().startswith(t), a.ident))
        return hits[:limit]

    def corridor(self, a: Airport, b: Airport, max_xt_nm: float,
                 min_rwy_ft: float, frac_lo: float = 0.0, frac_hi: float = 1.0) -> list[tuple[Airport, float, float]]:
        """Airports near the a->b course: (airport, cross_track_nm, along_track_nm)."""
        total = haversine_nm(a.lat, a.lon, b.lat, b.lon)
        out = []
        for ap in self.airports.values():
            if ap.ident in (a.ident, b.ident) or ap.longest_rwy_ft < min_rwy_ft:
                continue
            xt, at = cross_along_track(a.lat, a.lon, b.lat, b.lon, ap.lat, ap.lon)
            if abs(xt) <= max_xt_nm and frac_lo * total <= at <= frac_hi * total:
                out.append((ap, xt, at))
        return out
