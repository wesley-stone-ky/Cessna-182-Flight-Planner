# C182 Trip Planner

A personal trip-day planner for a 1985 Cessna 182R (GTN 750Xi), based at Lexington Blue Grass (KLEX).
You give it the days you could travel. It tries every departure time and route option against the
forecast and your personal minimums, then tells you which days work. It plans nonstop when the fuel
allows, and adds a fuel stop only when range, fatigue or weather calls for it.

Preloaded destinations:

| Ident | Airport | Notes |
|---|---|---|
| KLEX | Blue Grass, Lexington KY | home |
| KGGE | Georgetown County, SC | 6,005 ft rwy 05/23, GPS approaches |
| KSPG | Albert Whitted, St. Petersburg FL | "by the pier", 3,676 ft rwy 07/25 |
| KTPF | Peter O. Knight, Tampa FL | Davis Islands, 3,583 ft rwy 04/22 |

Any other public airport with a paved runway of 2,500 ft or more in the Southeast and Ohio Valley can
be typed in.

> **Screening tool, not a briefing.** Multi-day forecasts are model output and ceilings are *estimated*.
> Always get an official briefing (1800wxbrief.com, Garmin Pilot or ForeFlight), check NOTAMs/TFRs and
> fuel availability, and use your POH.

---

## What it does

1. **Builds route options.** It always tries nonstop first. If the leg won't fit your usable fuel plus
   reserve (with a 10 kt headwind allowance), or it runs longer than your max leg time, it finds fuel
   stops near the course. It prefers airports known to sell 100LL, with lit runways of 3,000 ft or
   more. It also keeps a few one-stop options as weather alternatives, such as west of the Smokies
   versus east.
2. **Pulls weather for every 20 nm of each route:**
   - **1–16 days:** Open-Meteo hourly forecasts (NOAA GFS and other models). These cover low/mid
     cloud, visibility, precipitation chance, CAPE, freezing level, and winds and temperatures at 925,
     850, 700 and 600 hPa.
   - **Next ~30 h:** aviationweather.gov TAFs (including TEMPO/PROB groups), METARs, AIRMET/SIGMETs
     and G-AIRMETs, which override the model near the airports.
3. **Pulls terrain** along a ±5 nm corridor (Open-Meteo elevation, cached on disk), so it knows when
   you're over the Cumberland Plateau and Blue Ridge.
4. **Flies each option** at each departure time in your window:
   - VFR hemispheric altitudes (14 CFR 91.159), checked against terrain and estimated cloud bases.
   - Climb, cruise and descent from the aircraft profile, with wind-corrected groundspeed segment by
     segment.
   - Fuel burn, with the reserve you choose.
5. **Screens against your minimums:**
   - airport ceiling and visibility
   - crosswind on the best runway and gusts
   - en-route ceilings versus terrain
   - convection (thunderstorm codes and CAPE)
   - icing risk (in cloud at or below +2 °C)
   - ridge-top winds
   - active advisories
   - daylight
   - fuel at landing
   - leg length
6. **Ranks the days** 🟢 GO / 🟡 MARGINAL / 🔴 NO-GO, with plain-English reasons, a forecast-confidence
   label, a map, an altitude profile and a nav log. It also exports:
   - `.fpl` files for Garmin Pilot and ForeFlight
   - `.gfp` files for the GTN SD card
   - a CSV nav log
   - a text trip summary

---

## Setup on WSL (one time)

```bash
mkdir -p ~/code && cd ~/code
# copy or unzip the project here so you have ~/code/c182-trip-planner
cd c182-trip-planner
bash setup_wsl.sh
```

The script installs Python venv support, git, the GitHub CLI, and Claude Code (or updates it if it's
already there). It then creates `.venv`, installs the dependencies and runs the offline tests.

Run the app:

```bash
source .venv/bin/activate
streamlit run app.py
```

Then open http://localhost:8501 in your Windows browser. WSL forwards localhost automatically.

### Put it on GitHub

```bash
gh auth login                       # GitHub.com → HTTPS → "Login with a web browser"
git init
git add .
git commit -m "C182 trip planner"
gh repo create c182-trip-planner --private --source=. --push
```

### Share it with your copilot

The easiest option is **Streamlit Community Cloud**, which is free:

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Choose **Create app**, pick the `c182-trip-planner` repo, branch `main`, and main file `app.py`.
3. In the app's **Share** settings, keep it private and invite your copilot's email.

Every `git push` updates the shared app. Your copilot only needs a browser, on a phone or tablet too.
The free plan allows one private app at a time, which is all you need here.

---

## ⚠️ Before you trust the numbers: edit `config/aircraft.yaml`

The aircraft profile holds **planning estimates** for a 182R with standard tanks: 56 gal usable,
65% power at about 11.8 gph and 126–136 KTAS. Open your POH and replace each `VERIFY` value:

- **Section 2 (Limitations):** usable fuel. Standard tanks on the 182Q/R are 61 gal total / 56 gal
  usable; long-range tanks are 88 gal usable. Check the placard on your fuel selector.
- **Section 5 (Performance):** time/fuel/distance to climb, cruise KTAS and GPH for your RPM/MP
  settings.

After a few flights, tune `ktas` and `gph` to what the 750Xi and your fuel totalizer actually show.

With 56 gal usable and a 1-hour reserve, **KLEX → St. Pete (≈625 nm) always needs one fuel stop.**
KLEX → Georgetown (≈385 nm) is usually nonstop but gets tight with a strong headwind. The app handles
both.

`config/minimums.yaml` holds your personal minimums. Everything in it can also be adjusted from the
sidebar.

---

## Project layout

```
app.py                     Streamlit UI
flightplanner/
  aircraft.py              performance model (climb/cruise/descent from YAML)
  airports.py              bundled airport + runway DB, fuel-stop search, favorites
  geo.py                   great circle, wind triangle, magnetic variation (WMM)
  terrain.py               corridor terrain (Open-Meteo elevation, cached)
  weather.py               Open-Meteo model forecast + aviationweather.gov products
  planner.py               routes, leg simulation, go/no-go screening, day search
  export.py                .fpl / .gfp / CSV / text brief
  demo.py                  synthetic forecast (offline demo + tests)
config/aircraft.yaml       YOUR airplane's numbers
config/minimums.yaml       YOUR personal minimums
data/airports.csv          OurAirports extract (public domain)
scripts/build_airport_data.py   refresh the airport extract
tests/                     offline pytest suite
```

Refresh the airport data occasionally with `python scripts/build_airport_data.py`.

## Data sources and references

- Open-Meteo forecast and elevation APIs (free for non-commercial use): https://open-meteo.com
- Aviation Weather Center Data API: https://aviationweather.gov/data/api/
- OurAirports (public domain): https://ourairports.com/data/
- 14 CFR 91.151 (VFR fuel), 91.155 (VFR weather minimums), 91.159 (VFR cruising altitudes),
  91.167 (IFR fuel), 91.103 (preflight action)
- FAA Personal Minimums Checklist (PAVE):
  https://www.faasafety.gov/files/gslac/courses/content/28/212/Personal%20Minimums%20Checklist.pdf
- FAA Aviation Weather Handbook (FAA-H-8083-28)

## Ideas for next steps (good Claude Code tasks)

- Add an IFR mode that checks approach minimums and alternates (91.169) for when you're current.
- Pull fuel prices and availability for stop candidates.
- Add a "send to my copilot" email/text summary.
- Add weight & balance for you, your copilot and bags.
