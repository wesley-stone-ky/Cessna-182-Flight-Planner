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

## Architecture
- `flightplanner/planner.py` is the core: `candidate_plans` → `attach_terrain` →
  `search_days(ctx, plans, start, end, earliest_hour, latest_hour)` → `DayResult`/`PlanResult`
  with `Finding`s (NO-GO / CAUTION).
- Weather: `weather.GridForecast` (NWS api.weather.gov grids as primary, merged with best-effort Open-Meteo for winds aloft and days 8-10; 0.5° grid; see weather.fetch_forecast and nws.py) plus
  `weather.AviationWeather` (TAF/METAR/AIRMET/SIGMET/G-AIRMET from aviationweather.gov).
  Set a User-Agent and keep AWC under 100 requests/min.
- Aircraft numbers come only from `config/aircraft.yaml`. Never hard-code performance.
- All times are UTC internally; display uses `Context.local()` (America/New_York).

- UI: `app.py` has 3 tabs (Trip planner, 10-day outlook, Before you go/live). Outlook logic is in
  `flightplanner/outlook.py`; the Leaflet live map (NEXRAD radar tiles from IEM, advisories, PIREPs) is in
  `flightplanner/livemap.py`.
- Fuel: 79 gal total confirmed by owner; usable set to 75 pending the selector placard.
  (FAA TCDS 3A13 lists 92 total / 88 usable integral tanks for the 182R — confirm the airplane's records.)
- Weight & balance: `flightplanner/wb.py`, limits/arms in `config/aircraft.yaml` (`weight_balance`).
  Limits from TCDS 3A13 Model 182R (3,100 lb takeoff, 2,950 lb landing, CG +33.0→+40.9 fwd, +46.0 aft).
  Empty weight/CG are EXAMPLE values until replaced from the airplane's W&B record.
  The planner flies with the fuel the load allows (`wb_res.fuel_gal` → `ac.usable_gal`) and
  checks W&B at each takeoff and landing (`Finding.kind == "wb"`).

## Rules
- Safety first: when unsure, make a check MORE conservative and say why in the finding text.
- Keep the "screening only — get an official briefing" disclaimer in the UI and exports.
- Don't add paid APIs or API keys without asking.
- Every new check needs a test in `tests/` that runs offline.
- Regulatory references: 14 CFR 91.151, 91.155, 91.159, 91.167, 91.169.
