"""Build the trimmed airport + runway dataset bundled with the app.

Source: OurAirports public-domain data (https://ourairports.com/data/),
mirrored on GitHub at davidmegginson/ourairports-data.

Run from the repo root:
    python scripts/build_airport_data.py            # downloads fresh copies
    python scripts/build_airport_data.py --offline  # uses data/raw/*.csv

Keeps public-use-looking airports in the Southeast/Ohio Valley with at least
one open, hard-surface runway of 2,500 ft or more. That is plenty for a C182
and keeps the file small enough to commit.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT_AIRPORTS = ROOT / "data" / "airports.csv"
OUT_RUNWAYS = ROOT / "data" / "runways.csv"

BASE = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/"

REGIONS = {
    "US-KY", "US-TN", "US-NC", "US-SC", "US-GA", "US-FL", "US-AL", "US-VA",
    "US-WV", "US-OH", "US-IN", "US-MS", "US-IL", "US-MO", "US-AR", "US-LA",
    "US-MD", "US-PA",
}
TYPES = {"small_airport", "medium_airport", "large_airport"}
HARD = ("ASP", "CON", "PEM", "BIT", "ASPH", "CONC", "TARMAC", "PAVED")
MIN_RWY_FT = 2500


def fetch(name: str, offline: bool) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / name
    if offline and path.exists():
        return path
    print(f"downloading {name} ...", file=sys.stderr)
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "c182-trip-planner"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def is_hard(surface: str) -> bool:
    s = (surface or "").upper()
    return any(h in s for h in HARD) and "GRASS" not in s and "TURF" not in s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    airports_raw = fetch("airports.csv", args.offline)
    runways_raw = fetch("runways.csv", args.offline)

    candidates: dict[str, dict] = {}
    with open(airports_raw, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["iso_region"] not in REGIONS or row["type"] not in TYPES:
                continue
            if "(Private)" in row["name"] or "private" in row["keywords"].lower():
                continue
            candidates[row["id"]] = row

    runways: dict[str, list[dict]] = {}
    with open(runways_raw, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["airport_ref"] not in candidates or row["closed"] == "1":
                continue
            try:
                length = int(float(row["length_ft"] or 0))
            except ValueError:
                continue
            if length < MIN_RWY_FT or not is_hard(row["surface"]):
                continue
            runways.setdefault(row["airport_ref"], []).append(row)

    OUT_AIRPORTS.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(OUT_AIRPORTS, "w", newline="", encoding="utf-8") as fa, \
         open(OUT_RUNWAYS, "w", newline="", encoding="utf-8") as fr:
        wa = csv.writer(fa)
        wr = csv.writer(fr)
        wa.writerow(["ident", "name", "type", "lat", "lon", "elev_ft", "region",
                     "city", "longest_rwy_ft", "lighted"])
        wr.writerow(["ident", "rwy", "heading_true", "length_ft", "surface", "lighted"])
        for aid, rws in runways.items():
            a = candidates[aid]
            ident = a["gps_code"] or a["icao_code"] or a["ident"]
            longest = max(int(float(r["length_ft"])) for r in rws)
            lighted = int(any(r["lighted"] == "1" for r in rws))
            wa.writerow([ident, a["name"], a["type"], a["latitude_deg"], a["longitude_deg"],
                         a["elevation_ft"] or 0, a["iso_region"], a["municipality"],
                         longest, lighted])
            n += 1
            for r in rws:
                for end, hdg in (("le", "le_heading_degT"), ("he", "he_heading_degT")):
                    rid = r[f"{end}_ident"]
                    h = r[hdg]
                    if not h and rid[:2].isdigit():
                        h = (int(rid[:2]) * 10) % 360  # fallback: magnetic-ish
                    if rid and h != "":
                        wr.writerow([ident, rid, round(float(h), 1),
                                     int(float(r["length_ft"])), r["surface"], r["lighted"]])
    print(f"wrote {n} airports -> {OUT_AIRPORTS.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
