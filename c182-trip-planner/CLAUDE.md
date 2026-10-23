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
- Weather: `weather.GridForecast` (Open-Meteo, snapped to a 0.25° grid) plus
  `weather.AviationWeather` (TAF/METAR/AIRMET/SIGMET/G-AIRMET from aviationweather.gov).
  Set a User-Agent and keep AWC under 100 requests/min.
- Aircraft numbers come only from `config/aircraft.yaml`. Never hard-code performance.
- All times are UTC internally; display uses `Context.local()` (America/New_York).

- UI: `app.py` has 3 tabs (Trip planner, 10-day outlook, Before you go/live). Outlook logic is in
  `flightplanner/outlook.py`; the Leaflet live map (NEXRAD radar tiles from IEM, advisories, PIREPs) is in
  `flightplanner/livemap.py`.
- Fuel: 79 gal total confirmed by owner; usable set to 75 pending the selector placard.

## Rules
- Safety first: when unsure, make a check MORE conservative and say why in the finding text.
- Keep the "screening only — get an official briefing" disclaimer in the UI and exports.
- Don't add paid APIs or API keys without asking.
- Every new check needs a test in `tests/` that runs offline.
- Regulatory references: 14 CFR 91.151, 91.155, 91.159, 91.167, 91.169.
