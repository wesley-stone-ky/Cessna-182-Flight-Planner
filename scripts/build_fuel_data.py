"""Build data/fuel.csv: which fuel each US public airport sells, and its attendance hours.

Source: FAA NASR 28-day subscription, CSV edition (public domain, official
Chart Supplement data): APT_BASE.csv (FUEL_TYPES) and APT_ATT.csv (attendance).
https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/

Run from the repo root:
    python scripts/build_fuel_data.py                  # downloads the current cycle (~10 MB)
    python scripts/build_fuel_data.py --zip APT.zip    # use a zip you already have

GitHub Actions runs this every week (.github/workflows/refresh-fuel.yml) and
commits only when the FAA has published a new cycle.

Fuel PRICES are not in any free official source, so the app links to AirNav
for current prices instead of copying them.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "fuel.csv"
BASE_URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/"
# Known cycle start; NASR cycles repeat every 28 days.
ANCHOR = date(2026, 10, 1)
# The FAA's CDN answers 503 to non-browser user agents.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def cycle_dates(today: date | None = None, back: int = 3) -> list[date]:
    """Current NASR cycle start date first, then earlier ones."""
    today = today or date.today()
    n = (today - ANCHOR).days // 28
    cur = ANCHOR + timedelta(days=28 * n)
    return [cur - timedelta(days=28 * i) for i in range(back)]


def cycle_url(d: date) -> str:
    return f"{BASE_URL}{d.day:02d}_{d.strftime('%b')}_{d.year}_APT_CSV.zip"


def download(today: date | None = None) -> tuple[bytes, date]:
    last = None
    for d in cycle_dates(today):
        url = cycle_url(d)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read(), d
        except Exception as e:  # not published yet / moved: try the previous cycle
            last = f"{url}: {e}"
    raise SystemExit(f"Could not download any recent NASR APT cycle ({last})")


def _open_csv(zf: zipfile.ZipFile, name: str):
    match = next((n for n in zf.namelist() if n.upper().endswith(name.upper())), None)
    if match is None:
        return None
    raw = zf.read(match)
    if raw[:2] == b"PK":            # some cycles nest a zip inside the zip
        return _open_csv(zipfile.ZipFile(io.BytesIO(raw)), name)
    return csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))


def build_rows(zip_bytes: bytes) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    base = _open_csv(zf, "APT_BASE.csv")
    if base is None:
        raise SystemExit("APT_BASE.csv not found in the zip")
    att: dict[str, list[str]] = {}
    att_rd = _open_csv(zf, "APT_ATT.csv")
    if att_rd is not None:
        for r in att_rd:
            sched = " ".join(x for x in (r.get("MONTH", ""), r.get("DAY", ""), r.get("HOUR", "")) if x).strip()
            if sched:
                att.setdefault(r["SITE_NO"].strip(), []).append(sched)
    rows = []
    for r in base:
        if (r.get("SITE_TYPE_CODE") or "").strip() != "A" or (r.get("FACILITY_USE_CODE") or "").strip() != "PU":
            continue
        arpt = (r.get("ARPT_ID") or "").strip().upper()
        icao = (r.get("ICAO_ID") or "").strip().upper()
        fuel = ",".join(t.strip() for t in (r.get("FUEL_TYPES") or "").split(",") if t.strip())
        rows.append({
            "ident": icao or arpt,
            "faa_id": arpt,
            "fuel_types": fuel,
            "attendance": "; ".join(att.get((r.get("SITE_NO") or "").strip(), []))[:120],
            "eff_date": (r.get("EFF_DATE") or "").strip(),
        })
    rows.sort(key=lambda x: x["ident"])
    return rows


def write(rows: list[dict], path: Path = OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ident", "faa_id", "fuel_types", "attendance", "eff_date"])
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="use this APT CSV zip instead of downloading")
    args = ap.parse_args()
    if args.zip:
        data = Path(args.zip).read_bytes()
    else:
        data, d = download()
        print(f"downloaded NASR cycle {d}", file=sys.stderr)
    rows = build_rows(data)
    write(rows)
    with_avgas = sum("100LL" in r["fuel_types"].split(",") for r in rows)
    print(f"wrote {len(rows)} public airports ({with_avgas} with 100LL) -> {OUT.relative_to(ROOT)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
