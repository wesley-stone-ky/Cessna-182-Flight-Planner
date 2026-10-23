"""Build the compact FAA aircraft registry extract used for tail-number lookup.

Source: FAA Releasable Aircraft Database (public, updated daily),
https://registry.faa.gov/database/ReleasableAircraft.zip

Run from the repo root:
    python scripts/build_faa_registry.py                 # downloads (~75 MB)
    python scripts/build_faa_registry.py --zip path.zip  # uses a copy you already have

Keeps only currently valid registrations of piston airplanes (single and
multi-engine) and only N-number, year, model and engine. Owner names and
addresses are in the FAA file but are deliberately NOT copied.

Writes data/faa_aircraft.csv.gz (n_number,year,model_code,engine_code),
data/faa_models.csv and data/faa_engines.csv. Refresh monthly at most; every
refresh adds a few MB to git history.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
# The FAA's CDN answers 503 to non-browser user agents.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
AIRPLANE_TYPES = {"4", "5"}     # fixed wing single / multi engine
PISTON = {"1"}                  # reciprocating


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        rd = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        next(rd)
        for r in rd:
            if r:
                yield [c.strip() for c in r]


def build(zf: zipfile.ZipFile) -> None:
    models = {}
    for r in _rows(zf, "ACFTREF.txt"):
        # CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,...
        if r[3] in AIRPLANE_TYPES and r[4] in PISTON:
            models[r[0]] = (r[1], r[2], r[3], r[6], r[7], r[8])
    engines = {r[0]: (r[1], r[2], r[4]) for r in _rows(zf, "ENGINE.txt")}   # CODE,MFR,MODEL,TYPE,HORSEPOWER
    used_m, used_e, n = set(), set(), 0
    DATA.mkdir(exist_ok=True)
    with gzip.open(DATA / "faa_aircraft.csv.gz", "wt", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(["n_number", "year", "model_code", "engine_code"])
        # MASTER: N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,...,STATUS CODE (col 20)
        rows = sorted((r for r in _rows(zf, "MASTER.txt") if r[2] in models and r[20] == "V"), key=lambda r: r[0])
        for r in rows:
            w.writerow([r[0], r[4], r[2], r[3]])
            used_m.add(r[2])
            used_e.add(r[3])
            n += 1
    with open(DATA / "faa_models.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "mfr", "model", "type_acft", "build_cert", "engines", "seats"])
        for c in sorted(used_m):
            w.writerow([c, *models[c]])
    with open(DATA / "faa_engines.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "mfr", "model", "hp"])
        for c in sorted(used_e):
            if c in engines:
                w.writerow([c, *engines[c]])
    size = (DATA / "faa_aircraft.csv.gz").stat().st_size
    print(f"{n:,} aircraft, {len(used_m):,} models, {len(used_e):,} engines; faa_aircraft.csv.gz {size / 1e6:.1f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", help="use an already-downloaded ReleasableAircraft.zip")
    args = ap.parse_args()
    if args.zip:
        data = Path(args.zip).read_bytes()
    else:
        print(f"downloading {URL} …", file=sys.stderr)
        req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=600) as r:
            data = r.read()
    build(zipfile.ZipFile(io.BytesIO(data)))


if __name__ == "__main__":
    main()
