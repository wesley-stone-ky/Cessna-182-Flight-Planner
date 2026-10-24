# CLAUDE.md — context for Claude Code sessions in this repo

## What this is
A personal flight trip-day planner (Python + Streamlit) for a 1985 Cessna 182R with a GTN 750Xi,
based at KLEX. Favorite destinations: KGGE (Georgetown SC), KSPG (Albert Whitted), KTPF (Peter O.
Knight). The pilot is returning to flying, currently VFR-only (formerly instrument rated), and
flies with a copilot. The app picks good travel days and routes; it is a screening tool, not a
legal weather briefing.

## Commands
- Setup: `bash setup_wsl.sh` (creates `.venv`)
- Run: `source .venv/bin/activate && streamlit run app.py`
- Tests: `python -m pytest -q` (offline; uses synthetic weather and fallback terrain)
- Refresh airports: `python scripts/build_airport_data.py`
- Refresh FAA registry: `python scripts/build_faa_registry.py`

## Architecture
- `flightplanner/planner.py` is the core: `candidate_plans` → `attach_terrain` →
  `search_days(ctx, plans, start, end, earliest_hour, latest_hour)` → `DayResult`/`PlanResult`
  with `Finding`s (NO-GO / CAUTION).
- Weather: `weather.GridForecast` (NWS api.weather.gov grids only, 7 days, 0.5° grid; see weather.fetch_forecast,
  weather.nws_series and nws.py) plus
  `weather.AviationWeather` (TAF/METAR/AIRMET/SIGMET/G-AIRMET from aviationweather.gov).
  Set a User-Agent and keep AWC under 100 requests/min.
- Aircraft numbers come only from the type catalog (`config/aircraft_types/<family>.yaml`, base +
  variants) and the owner's profile (`config/aircraft.yaml`: `type:` + overrides, which win). Never
  hard-code performance. Keep catalog types `verified: false` until checked against a real POH.
  Unconfirmed fuel uses the smallest tank option. Dad's 182R is pinned by a golden test.
- Tail numbers: `flightplanner/registry.py` (FAA registry extract in data/, no owner data; rebuild
  with `python scripts/build_faa_registry.py`, monthly at most).
- All times are UTC internally; display uses `Context.local()` (America/New_York).

- NWS has no winds/humidity aloft: `WxPoint.aloft_estimated` is set, and the planner then gives no tailwind
  credit and estimates icing conservatively. Don't add Open-Meteo back (its free tier is non-commercial).
- Terrain: AWS Terrain Tiles (terrain.py), with a conservative offline fallback.
- Weekend email: `flightplanner/weekend.py` + `scripts/weekend_email.py`, run by
  `.github/workflows/weekend-email.yml`. Subscriber addresses live only in the SUBSCRIBERS_JSON secret.
- Web app (multi-user): `web/` (FastAPI + Jinja, SQLite via `web/db.py`, signed tokens in `web/auth.py`,
  `web/pilot.py` builds Minimums/Aircraft per user: onboarding preset from `config/minimum_presets.yaml`
  + fine-tuning clamped to legal floors, crosswind capped at the airplane's demonstrated value).
  Trip mode: `web/trips.py` (same planner, 15-min cache, Garmin zip export). Store is SQLite locally,
  Postgres when DATABASE_URL is set (`tests/test_store.py` runs on both if TEST_DATABASE_URL is set).
  Hosting: `render.yaml` (Render free web service + Neon Postgres). Requires SECRET_KEY on https.
  Run: `uvicorn web.app:create_app --factory`. Tests in `tests/test_web.py` (offline).
- Daily verdicts (`outlook.check_hour`, used by Can I fly? and the email) check gust-based crosswind on the
  best runway, like the trip planner, and use the WORSE of TAF (incl. TEMPO/PROB) and NWS grid for the
  hours a TAF covers. A TAF must never make an answer less cautious.
- UI: `app.py` has 3 tabs (Trip planner, 7-day outlook, Before you go/live). Outlook logic is in
  `flightplanner/outlook.py`; the Leaflet live map (NEXRAD radar tiles from IEM, advisories, PIREPs) is in
  `flightplanner/livemap.py`.
- Fuel: 79 gal total confirmed by owner; usable set to 75 pending the selector placard.
  (FAA TCDS 3A13 lists 92 total / 88 usable integral tanks for the 182R — confirm the airplane's records.)
- Weight & balance: `flightplanner/wb.py`, limits/arms in `config/aircraft.yaml` (`weight_balance`).
  Limits from TCDS 3A13 Model 182R (3,100 lb takeoff, 2,950 lb landing, CG +33.0→+40.9 fwd, +46.0 aft).
  Empty weight/CG are EXAMPLE values until replaced from the airplane's W&B record.
  The planner flies with the fuel the load allows (`wb_res.fuel_gal` → `ac.usable_gal`) and
  checks W&B at each takeoff and landing (`Finding.kind == "wb"`).

- Day/night: `minimums.day_only` (UI "Night flying": Day or night / Land before dark). Night allowed is
  the default; `_night_checks` in planner.py then adds the 45-min night reserve (91.151), a 61.57(b)
  night-currency caution unless `night_current`, and a caution for night legs over terrain > 3,000 ft.

- Fuel availability: `flightplanner/fuel.py` reads `data/fuel.csv` (FAA NASR APT_BASE FUEL_TYPES + APT_ATT
  attendance), built by `scripts/build_fuel_data.py` and refreshed weekly by
  `.github/workflows/refresh-fuel.yml`. Stops the FAA says lack 100LL are never used; unknown falls back to
  `airports.KNOWN_FUEL`. Prices are not scraped (no free official source) — the UI links to AirNav.

## Rules
- Safety first: when unsure, make a check MORE conservative and say why in the finding text.
- Keep the "screening only — get an official briefing" disclaimer in the UI and exports.
- Don't add paid APIs or API keys without asking.
- Every new check needs a test in `tests/` that runs offline.
- Regulatory references: 14 CFR 91.151, 91.155, 91.159, 91.167, 91.169.
