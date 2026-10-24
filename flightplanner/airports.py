"""Airport database (bundled OurAirports extract) and fuel-stop search."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache

from .config import DATA_DIR
from .geo import cross_along_track, haversine_nm, wind_components

# Airports along the Ohio Valley -> Southeast corridors that are widely known to
# sell 100LL (elsewhere in the US nothing is marked yet; fuel stops still work). Fuel availability and hours change: ALWAYS confirm on AirNav or
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
    tz: str = "America/New_York"   # IANA time zone of the airport

    @property
    def avgas(self) -> bool | None:
        """100LL per the FAA airport data (data/fuel.csv): True / False / None = unknown."""
        from .fuel import avgas_status
        return avgas_status(self.ident)

    @property
    def known_fuel(self) -> bool:
        status = self.avgas
        return self.ident in KNOWN_FUEL if status is None else status

    @property
    def label(self) -> str:
        return f"{self.ident} – {self.name} ({self.city})"

    @property
    def state(self) -> str:
        return self.region.replace("US-", "")


def best_runway(ap: Airport, wdir: float | None, kt: float) -> tuple[str, float, float] | None:
    """(runway, crosswind, headwind) with the least crosswind for a wind of `kt`.

    Variable wind is treated as straight across. None = no runway >= 2500 ft without a tailwind."""
    if kt <= 0:
        return ("any", 0.0, 0.0)
    if wdir is None:
        return ("any", kt, 0.0)
    best = None
    for rw in ap.runways:
        if rw.length_ft < 2500:
            continue
        head, cross = wind_components(rw.heading_true, wdir, kt)
        if head < -3:
            continue
        if best is None or cross < best[1]:
            best = (rw.ident, cross, head)
    return best


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
                    runways=tuple(rws.get(ident, ())), tz=r.get("tz") or "America/New_York")
        return cls(out)

    def get(self, ident: str) -> Airport:
        key = ident.strip().upper()
        if key in self.airports:
            return self.airports[key]
        if len(key) == 3 and "K" + key in self.airports:
            return self.airports["K" + key]
        raise KeyError(f"Airport {ident!r} not found in the bundled database "
                       "(US public airports with a paved runway of 2,500 ft or more).")

    def search(self, text: str, limit: int = 15) -> list[Airport]:
        t = text.strip().lower()
        if not t:
            return []
        hits = [a for a in self.airports.values()
                if t in a.ident.lower() or t in a.name.lower() or t in a.city.lower()]
        size = {"large_airport": 0, "medium_airport": 1}
        # Code matches first; then, for a town name, the bigger airports (what most people mean by "Lexington").
        hits.sort(key=lambda a: (a.ident.lower() != t and a.ident.lower() != "k" + t,
                                 not a.ident.lower().startswith(t), size.get(a.type, 2), -a.longest_rwy_ft, a.ident))
        return hits[:limit]

    def nearby(self, ap: Airport, max_nm: float = 40.0, limit: int = 8) -> list[tuple[str, float]]:
        """Nearby stations to borrow a METAR/TAF from: bigger airports first (they
        are the ones with TAFs), then by distance."""
        cands = []
        for o in self.airports.values():
            if o.ident == ap.ident or not o.ident.startswith("K") or len(o.ident) != 4:
                continue
            d = haversine_nm(ap.lat, ap.lon, o.lat, o.lon)
            if d <= max_nm:
                cands.append((o.type == "small_airport", d, o.ident))
        cands.sort()
        return [(i, d) for _, d, i in cands[:limit]]

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
