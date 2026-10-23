"""C182 Trip Planner — Streamlit UI.

Run locally:   streamlit run app.py
Tabs:
  1. Trip planner      – which days/times/routes work in your travel window
  2. 10-day outlook    – daily weather at each airport and along the route
  3. Before you go     – live METAR/TAF/radar/advisories/PIREPs + go/no-go right now
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st
import streamlit.components.v1 as components

from flightplanner.aircraft import Aircraft
from flightplanner.airports import FAVORITES, AirportDB
from flightplanner.config import load_minimums
from flightplanner.demo import synthetic_forecast
from flightplanner.export import leg_fpl, leg_gfp, navlog_csv, text_brief
from flightplanner.geo import haversine_nm
from flightplanner.livemap import live_map_html
from flightplanner.outlook import airport_outlook, hourly_rows, route_outlook
from flightplanner.planner import (NOGO, CAUTION, Context, attach_terrain, candidate_plans, evaluate_plan,
                                   forecast_points, route_weather_rows, search_days)
from flightplanner.terrain import Terrain
from flightplanner.weather import AviationWeather, decode_metar, fetch_model_forecast

st.set_page_config(page_title="C182 Trip Planner", page_icon="✈️", layout="wide")

STATUS_ICON = {"GO": "🟢 GO", "MARGINAL": "🟡 MARGINAL", "NO-GO": "🔴 NO-GO"}
VERDICT_ICON = {"Good": "🟢 Good", "Fair": "🟡 Fair", "Poor": "🔴 Poor", "No data": "⚪ No data"}
CAT_BADGE = {"VFR": ":green-badge[VFR]", "MVFR": ":blue-badge[MVFR]", "IFR": ":red-badge[IFR]",
             "LIFR": ":violet-badge[LIFR]"}
TZ = "America/New_York"
ZONE = ZoneInfo(TZ)

if "sources" not in st.session_state:
    st.session_state["sources"] = {}


def note_source(name: str, ok: bool, detail: str = "") -> None:
    st.session_state["sources"][name] = (ok, datetime.now(timezone.utc), detail)


# ---------------------------------------------------------------------------
# Cached data access
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db() -> AirportDB:
    return AirportDB.load()


@st.cache_resource
def get_terrain() -> Terrain:
    return Terrain()


@st.cache_data(ttl=1800, show_spinner=False)
def get_model_forecast(points: tuple[tuple[float, float], ...]):
    return fetch_model_forecast(points, ttl_s=1800)


@st.cache_data(ttl=600, show_spinner=False)
def get_model_forecast_live(points: tuple[tuple[float, float], ...]):
    return fetch_model_forecast(points, ttl_s=600)


@st.cache_data(ttl=300, show_spinner=False)
def get_awc(airport_ids: tuple[str, ...], pirep_points: tuple[tuple[str, float], ...] = ()):
    """METARs/TAFs (borrowing the nearest station when an airport has none),
    advisories and PIREPs. Cached 5 minutes."""
    awc = AviationWeather(ttl_s=240)
    aps = [get_db().get(i) for i in airport_ids]
    metars, tafs = awc.for_airports(aps, get_db().nearby)
    advs = awc.advisories()
    pireps = awc.pireps(list(pirep_points)) if pirep_points else []
    status = {k: (v.ok, v.fetched_at, v.count, v.error) for k, v in awc.status.items()}
    return metars, tafs, advs, pireps, status, datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Sidebar: aircraft + minimums + data status
# ---------------------------------------------------------------------------
base_ac = Aircraft.load()
base_mins = load_minimums()

with st.sidebar:
    st.header("✈️ Aircraft")
    st.caption(f"{base_ac.raw.get('model')} · {base_ac.raw.get('avionics')}")
    settings = list(base_ac.raw["cruise_settings"].keys())
    setting = st.selectbox("Cruise power", settings,
                           index=settings.index(base_ac.raw.get("default_cruise_setting", settings[0])))
    usable = st.number_input("Usable fuel (gal)", 20.0, 120.0, float(base_ac.usable_gal), 1.0,
                             help="The number on the fuel selector placard ('BOTH – xx GAL').")
    with st.expander("Performance numbers in use"):
        cs = base_ac.raw["cruise_settings"][setting]
        st.write(f"Fuel flow: **{cs['gph']} gph**")
        st.dataframe(pd.DataFrame({"Altitude": list(cs["ktas"].keys()), "KTAS": list(cs["ktas"].values())}),
                     hide_index=True, width="stretch")
        st.caption("Edit `config/aircraft.yaml` with the numbers from YOUR POH, Section 5.")

    st.header("🧭 Personal minimums")
    rules = st.radio("Flight rules", ["VFR", "IFR"], horizontal=True, index=0 if not base_mins.ifr else 1,
                     help="Use IFR only when you're instrument rated and current.")
    if rules == "IFR":
        st.warning("IFR mode screens for icing, convection and airport weather only. "
                   "It does not check approach minimums or alternates.")
    cig = st.slider("Airport ceiling (ft AGL)", 1000, 5000, int(base_mins.airport_min_ceiling_ft_agl), 500)
    vis = st.slider("Airport visibility (sm)", 3, 10, int(base_mins.airport_min_vis_sm))
    xw = st.slider("Max crosswind (kt)", 5, 15, int(base_mins.max_crosswind_kt))
    reserve = st.select_slider("Fuel reserve (min)", [30, 45, 60, 75, 90], int(base_mins.reserve_minutes))
    max_leg = st.slider("Longest leg you want to sit (h)", 1.5, 5.0, float(base_mins.max_leg_hours), 0.5)
    day_only = st.checkbox("Daylight only", base_mins.day_only)
    ground = st.slider("Time on the ground at a fuel stop (min)", 20, 120, int(base_mins.fuel_stop_ground_minutes), 5)

    st.header("🛰️ Live data")
    # Offline demo forecast is for development only: add ?demo=1 to the URL to use it.
    demo = st.query_params.get("demo") == "1"
    if demo:
        st.warning("Demo weather is ON (made-up forecast). Remove ?demo=1 from the address to use live data.")
    status_box = st.container()

ac = Aircraft.load(cruise_setting=setting, overrides={"fuel.usable_gal": usable})
mins = load_minimums({
    "flight_rules": rules, "airport_min_ceiling_ft_agl": cig, "airport_min_vis_sm": vis,
    "max_crosswind_kt": xw, "reserve_minutes": reserve, "max_leg_hours": max_leg,
    "day_only": day_only, "fuel_stop_ground_minutes": ground,
})
db = get_db()


@st.cache_data(show_spinner=False)
def get_plans(dep_ident: str, dest_ident: str, settings_key: tuple):
    dep, dest = db.get(dep_ident), db.get(dest_ident)
    plans = candidate_plans(db, dep, dest, ac, mins)
    terrain = get_terrain()
    attach_terrain(plans, terrain, mins)
    return plans, terrain.used_fallback


def plans_for(dep_ident: str, dest_ident: str):
    key = (usable, setting, reserve, max_leg, rules, cig, vis, xw, day_only)
    return get_plans(dep_ident, dest_ident, key)


def forecast_for(plans, live: bool = False):
    pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
    now = datetime.now(timezone.utc)
    if demo:
        note_source("Model forecast (Open-Meteo)", True, "DEMO data")
        return synthetic_forecast(pts, start=now - timedelta(hours=1), days=16)
    key = tuple(sorted({(round(a, 3), round(b, 3)) for a, b in pts}))
    try:
        fc = (get_model_forecast_live if live else get_model_forecast)(key)
        note_source("Model forecast (Open-Meteo)", True,
                    f"{len(fc.series)} grid points, through {fc.horizon.astimezone(ZONE):%a %b %-d}"
                    if fc.horizon else "")
        return fc
    except Exception as e:
        note_source("Model forecast (Open-Meteo)", False, str(e)[:150])
        raise


def as_range(w) -> tuple[date, date]:
    if isinstance(w, (list, tuple)):
        return (w[0], w[-1]) if len(w) > 1 else (w[0], w[0])
    return w, w


def ago(dt: datetime | None) -> str:
    if not dt:
        return "?"
    mins_ = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    return "just now" if mins_ < 1 else f"{mins_} min ago" if mins_ < 120 else f"{mins_ // 60} h ago"


# ---------------------------------------------------------------------------
# Header + shared trip input
# ---------------------------------------------------------------------------
st.title("C182 Trip Planner")

fav_labels = [f"{k} — {v}" for k, v in FAVORITES.items()] + ["Other airport…"]


def airport_picker(label: str, default_idx: int, key: str) -> str:
    choice = st.selectbox(label, fav_labels, index=default_idx, key=key)
    if choice == "Other airport…":
        txt = st.text_input(f"{label}: identifier or name", key=f"{key}_txt", placeholder="e.g. KCHA or Chattanooga")
        hits = db.search(txt) if txt else []
        if hits:
            pick = st.selectbox("Matches", [h.label for h in hits], key=f"{key}_pick")
            return pick.split(" – ")[0]
        return txt.strip().upper()
    return choice.split(" — ")[0]


c1, c2 = st.columns(2)
with c1:
    dep_id = airport_picker("From", 0, "dep")
with c2:
    dest_id = airport_picker("To", 1, "dest")

valid_pair = True
try:
    db.get(dep_id), db.get(dest_id)
except KeyError as e:
    st.error(str(e).strip('"'))
    valid_pair = False

tab_plan, tab_outlook, tab_live = st.tabs(["🗓️ Trip planner", "🌤️ 10-day outlook", "🛫 Before you go (live)"])


# ---------------------------------------------------------------------------
# Shared renderers
# ---------------------------------------------------------------------------
def day_table(ctx, days):
    rows = []
    for d in days:
        b = d.best
        if b is None:
            rows.append({"Day": d.day.strftime("%a %b %-d"), "Verdict": "⚪ no data", "Leave": "", "Route": "",
                         "Arrive": "", "Flying": "", "Confidence": "", "Why": "Outside forecast range"})
            continue
        rows.append({
            "Day": d.day.strftime("%a %b %-d"), "Verdict": STATUS_ICON[b.status],
            "Leave": ctx.local(b.dep_time).strftime("%-I:%M %p"), "Route": b.plan.label,
            "Arrive": ctx.local(b.arr_time).strftime("%-I:%M %p"), "Flying": f"{b.flight_hours:.1f} h",
            "Confidence": b.confidence, "Why": " · ".join(b.top_reasons(2)) or "Meets your minimums",
        })
    return pd.DataFrame(rows)


def route_map(r):
    paths = [{"path": [[lon, lat] for lat, lon, _ in lr.leg.samples]} for lr in r.legs]
    pts = [{"lon": a.lon, "lat": a.lat, "ident": a.ident, "name": a.name} for a in r.plan.airports]
    lats, lons = [p["lat"] for p in pts], [p["lon"] for p in pts]
    view = pdk.ViewState(latitude=(min(lats) + max(lats)) / 2, longitude=(min(lons) + max(lons)) / 2,
                         zoom=5 if max(lats) - min(lats) < 6 else 4.4)
    return pdk.Deck(map_style=pdk.map_styles.CARTO_DARK, initial_view_state=view, tooltip={"text": "{ident}\n{name}"}, layers=[
        pdk.Layer("PathLayer", paths, get_path="path", get_color=[56, 189, 248], width_min_pixels=3),
        pdk.Layer("ScatterplotLayer", pts, get_position=["lon", "lat"], get_radius=9000,
                  get_fill_color=[251, 146, 60], pickable=True),
        pdk.Layer("TextLayer", pts, get_position=["lon", "lat"], get_text="ident", get_size=14,
                  get_pixel_offset=[0, -18], get_color=[226, 232, 240])])


def profile_chart(r):
    rows, offset = [], 0.0
    for lr in r.legs:
        for p in lr.profile:
            rows.append({"nm": offset + p.dist_nm, "Terrain (corridor max)": p.terrain_ft,
                         "Your altitude": p.alt_ft, "Est. cloud base": p.ceiling_msl_ft})
        offset += lr.leg.dist_nm
    df = pd.DataFrame(rows)
    base = alt.Chart(df).encode(x=alt.X("nm:Q", title="Distance (nm)"))
    terr = base.mark_area(color="#78716c", opacity=0.7).encode(y=alt.Y("Terrain (corridor max):Q", title="Feet MSL"))
    me = base.mark_line(color="#38bdf8", strokeWidth=3).encode(y="Your altitude:Q")
    cl = base.mark_point(color="#e2e8f0", filled=True, size=60, shape="triangle-down").encode(
        y="Est. cloud base:Q", tooltip=["nm", "Est. cloud base"])
    return (terr + me + cl).properties(height=260)


def findings_block(r):
    nogo = [f for f in r.findings if f.level == NOGO]
    caution = [f for f in r.findings if f.level == CAUTION]
    if nogo:
        st.error("**Deal-breakers**\n\n" + "\n".join(f"- Leg {f.leg + 1}: {f.text}" for f in nogo))
    if caution:
        st.warning("**Look closer**\n\n" + "\n".join(f"- Leg {f.leg + 1}: {f.text}" for f in caution))
    if not nogo and not caution:
        st.success("Nothing in the forecast breaks your minimums on this plan.")


def plan_detail(ctx, r, key: str):
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Verdict", STATUS_ICON[r.status])
    m2.metric("Leave", ctx.local(r.dep_time).strftime("%-I:%M %p"))
    m3.metric("Arrive", ctx.local(r.arr_time).strftime("%-I:%M %p"))
    m4.metric("Flying time", f"{r.flight_hours:.1f} h")
    m5.metric("Fuel stops", len(r.plan.stops))
    st.caption(f"Forecast confidence: **{r.confidence}** · {r.plan.total_nm:.0f} nm total")
    findings_block(r)

    left, right = st.columns([3, 2])
    with left:
        st.pydeck_chart(route_map(r), width="stretch")
    with right:
        st.dataframe(pd.DataFrame([{
            "Leg": f"{lr.leg.dep.ident} → {lr.leg.arr.ident}", "Dist": f"{lr.leg.dist_nm:.0f} nm",
            "Alt": f"{lr.alt_ft:,.0f}", "MC/MH": f"{lr.leg.mag_course:03.0f}/{lr.mag_heading:03.0f}",
            "GS": f"{lr.avg_gs_kt:.0f} ({lr.avg_wind_component_kt:+.0f})", "ETE": f"{lr.minutes:.0f} min",
            "Fuel used": f"{lr.fuel_used_gal:.1f}", "Land with": f"{lr.fuel_at_landing_gal:.1f} gal",
        } for lr in r.legs]), hide_index=True, width="stretch")
        for lr in r.legs:
            if lr.dep_wx:
                st.caption(f"**{lr.leg.dep.ident}** at {ctx.local(lr.dep_time):%-I:%M %p}: {lr.dep_wx.summary()}")
        last = r.legs[-1]
        if last.arr_wx:
            st.caption(f"**{last.leg.arr.ident}** at {ctx.local(last.arr_time):%-I:%M %p}: {last.arr_wx.summary()}")

    st.markdown("**Weather along the route at your ETA** (forecast at each 20-nm point)")
    st.dataframe(pd.DataFrame(route_weather_rows(ctx, r)), hide_index=True, width="stretch")

    st.markdown("**Altitude profile** — blue is your planned altitude, grey is the highest terrain within "
                f"{mins.corridor_halfwidth_nm:.0f} nm of course, triangles are estimated cloud bases (MSL).")
    st.altair_chart(profile_chart(r), width="stretch")

    ids = [a.ident for a in r.plan.airports]
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        for lr in r.legs:
            n = f"{lr.leg.dep.ident}-{lr.leg.arr.ident}"
            z.writestr(f"{n}.fpl", leg_fpl(lr.leg))
            z.writestr(f"{n}.gfp", leg_gfp(lr.leg))
    b1, b2, b3 = st.columns(3)
    stamp = ctx.local(r.dep_time).strftime("%Y%m%d-%H%M")
    b1.download_button("📄 Trip summary (.txt)", text_brief(ctx, r), f"trip_{stamp}_{'-'.join(ids)}.txt",
                       width="stretch", key=f"{key}_txt")
    b2.download_button("🧾 Nav log (.csv)", navlog_csv(ctx, r), f"navlog_{stamp}_{'-'.join(ids)}.csv",
                       width="stretch", key=f"{key}_csv")
    b3.download_button("🛩️ Flight plans for Garmin (.zip)", zbuf.getvalue(), f"fpl_{stamp}_{'-'.join(ids)}.zip",
                       width="stretch", key=f"{key}_zip",
                       help=".fpl imports into Garmin Pilot / ForeFlight; .gfp goes on an SD card for the GTN.")


# ---------------------------------------------------------------------------
# Tab 1: Trip planner
# ---------------------------------------------------------------------------
def run_direction(dep_ident: str, dest_ident: str, win, hrs):
    plans, fallback = plans_for(dep_ident, dest_ident)
    if not plans:
        raise ValueError("No route works within your fuel and leg-length limits. "
                         "Try a longer max leg, smaller reserve, or check the usable fuel setting.")
    notes = []
    fc = forecast_for(plans)
    now = datetime.now(timezone.utc)
    tafs, advs = {}, []
    if demo:
        notes.append("Demo weather — not a real forecast.")
    else:
        ids = tuple(sorted({a.ident for p in plans for a in p.airports}))
        _, tafs, advs, _, status, _ = get_awc(ids)
        bad = [k for k, v in status.items() if not v[0]]
        if bad:
            notes.append("Couldn't reach aviationweather.gov for: " + ", ".join(bad))
        borrowed = sorted({f"{k} uses {v['station']}" for k, v in tafs.items() if v.get("station") != k})
        if borrowed:
            notes.append("No TAF at some airports, so the nearest one is used: " + "; ".join(borrowed))
    if fallback:
        notes.append("Couldn't reach the terrain service; using conservative built-in terrain heights.")
    ctx = Context(ac, mins, fc, db, tafs=tafs, advisories=advs, tz=TZ, now=now)
    start, end = as_range(win)
    days = search_days(ctx, plans, start, end, hrs[0], hrs[1])
    return dict(ctx=ctx, plans=plans, days=days, notes=notes, dep=db.get(dep_ident), dest=db.get(dest_ident))


def show_direction(title: str, res: dict, key: str):
    ctx, days = res["ctx"], res["days"]
    st.subheader(title)
    for n in res["notes"]:
        st.caption(f"ℹ️ {n}")
    st.caption("Weather used: model forecast (Open-Meteo, NOAA GFS/HRRR and others) for every hour and point; "
               "TAFs and AIRMET/SIGMETs from aviationweather.gov override it inside ~30 hours.")
    if not any(d.best for d in days):
        st.warning("No departures fall inside the forecast range for those days.")
        return
    st.dataframe(day_table(ctx, days), hide_index=True, width="stretch")

    good = [d for d in days if d.best and d.status == "GO"] or [d for d in days if d.best]
    labels = [d.day.strftime("%a %b %-d") for d in days if d.best]
    default = labels.index(good[0].day.strftime("%a %b %-d")) if good else 0
    pick = st.selectbox("Look closer at", labels, index=default, key=f"{key}_day")
    d = next(x for x in days if x.best and x.day.strftime("%a %b %-d") == pick)
    opts = d.options[:4]
    opt_labels = [f"{o.plan.label} — {STATUS_ICON[o.status]}, leave {ctx.local(o.dep_time):%-I:%M %p}" for o in opts]
    oi = st.radio("Route options for this day", range(len(opts)), format_func=lambda i: opt_labels[i],
                  key=f"{key}_opt")
    plan_detail(ctx, opts[oi], key)
    if d.by_hour:
        with st.expander("Every departure time that day"):
            st.dataframe(pd.DataFrame([{
                "Leave": ctx.local(h.dep_time).strftime("%-I:%M %p"), "Verdict": STATUS_ICON[h.status],
                "Route": h.plan.label, "Arrive": ctx.local(h.arr_time).strftime("%-I:%M %p"),
                "Why": " · ".join(h.top_reasons(1)) or "—"} for h in d.by_hour]),
                hide_index=True, width="stretch")


with tab_plan:
    st.caption("Pick your travel window. The planner tries every departure time and route option against the "
               "forecast and your minimums: nonstop if possible, fuel stops only when range or weather needs them.")
    today = datetime.now(ZONE).date()
    c3, c4 = st.columns([2, 2])
    with c3:
        window = st.date_input("Days you could leave", (today + timedelta(days=1), today + timedelta(days=7)),
                               min_value=today, max_value=today + timedelta(days=15), format="MM/DD/YYYY")
    with c4:
        hours = st.slider("Departure window (local time)", 5, 19, (8, 13), format="%d:00")
    ret = st.checkbox("Plan the trip home too")
    if ret:
        c5, c6 = st.columns([2, 2])
        with c5:
            ret_window = st.date_input("Days you could come home",
                                       (today + timedelta(days=4), today + timedelta(days=11)),
                                       min_value=today, max_value=today + timedelta(days=15),
                                       format="MM/DD/YYYY", key="retwin")
        with c6:
            ret_hours = st.slider("Return departure window", 5, 19, (9, 14), format="%d:00", key="rethrs")

    if st.button("Find the best days", type="primary", width="stretch", disabled=not valid_pair):
        try:
            with st.spinner("Pulling forecasts and checking every departure time…"):
                out = {"out": run_direction(dep_id, dest_id, window, hours)}
                if ret:
                    out["back"] = run_direction(dest_id, dep_id, ret_window, ret_hours)
            st.session_state["results"] = out
        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.info("The weather service may be busy or unreachable. Wait a minute and try again.")

    results = st.session_state.get("results")
    if results:
        o = results["out"]
        show_direction(f"{o['dep'].ident} → {o['dest'].ident}", o, "out")
        if "back" in results:
            st.divider()
            b = results["back"]
            show_direction(f"Home: {b['dep'].ident} → {b['dest'].ident}", b, "back")


# ---------------------------------------------------------------------------
# Tab 2: 10-day outlook
# ---------------------------------------------------------------------------
def outlook_table(rows):
    return pd.DataFrame([{
        "Day": o.day.strftime("%a %b %-d"),
        "Flying": VERDICT_ICON[o.verdict],
        "Good hours": f"{o.flyable_hours}/{o.daylight_hours}" if o.daylight_hours else "—",
        "Best window": o.best_window,
        "Lowest ceiling": "—" if o.verdict == "No data" else ("none" if o.low_ceiling_ft is None
                                                               else f"{round(o.low_ceiling_ft, -2):,.0f} ft"),
        "Wind / gust": "—" if o.verdict == "No data" else f"{o.max_wind_kt:.0f} / {o.max_gust_kt:.0f} kt",
        "Rain": "—" if o.verdict == "No data" else f"{o.rain_pct:.0f}%",
        "Storms": o.storms,
        "Winds @6000": o.wind_6000,
        "Hi/Lo °F": "—" if o.hi_c is None else f"{o.hi_c * 9 / 5 + 32:.0f}/{o.lo_c * 9 / 5 + 32:.0f}",
        "Summary": o.summary,
    } for o in rows])


with tab_outlook:
    st.caption("Daily weather for the next 10 days. 'Good hours' counts daylight hours that meet your airport "
               "minimums. Days 1–3 are fairly reliable, 4–7 give the general idea, and 8–10 show trends only.")
    if valid_pair:
        try:
            plans, _ = plans_for(dep_id, dest_id)
            if plans:
                fc = forecast_for(plans[:3])
                main = plans[0]
                rt = route_outlook(fc, main.legs, mins, TZ, days=10)
                st.subheader(f"Along the route: {main.route_string} ({main.label})")
                st.dataframe(pd.DataFrame([{
                    "Day": r_["day"].strftime("%a %b %-d"), "En route": VERDICT_ICON[r_["verdict"]],
                    "Worst cloud room over terrain": "—" if r_["ceiling_margin"] is None
                    else (f"{r_['ceiling_margin']:+,.0f} ft" if r_["ceiling_margin"] < 5000 else "plenty"),
                    "Max rain chance": "—" if r_["rain"] is None else f"{r_['rain']:.0f}%",
                    "Storms": r_["storms"]} for r_ in rt]), hide_index=True, width="stretch")
                st.caption("'Cloud room' = estimated cloud base minus (terrain + your clearance + 500 ft), "
                           "worst spot on the route at 10 AM, 1 PM and 4 PM. Negative means VFR won't fit there.")
                shown = []
                for ap in [main.airports[0]] + main.stops + [main.airports[-1]]:
                    if ap.ident in shown:
                        continue
                    shown.append(ap.ident)
                    st.subheader(f"{ap.ident} — {ap.name}")
                    rows = airport_outlook(fc, ap, mins, TZ, days=10)
                    st.dataframe(outlook_table(rows), hide_index=True, width="stretch")
                    with st.expander(f"Hour by hour at {ap.ident}"):
                        dsel = st.selectbox("Day", [o.day for o in rows if o.verdict != "No data"],
                                            format_func=lambda d_: d_.strftime("%a %b %-d"), key=f"hr_{ap.ident}")
                        st.dataframe(pd.DataFrame(hourly_rows(fc, ap, dsel, TZ)), hide_index=True, width="stretch")
                        st.caption("Ceilings and flight categories here are model estimates, not observations.")
        except Exception as e:
            st.error(f"Couldn't load the outlook: {e}")
            st.info("The weather service may be busy or unreachable. Wait a minute and try again.")


# ---------------------------------------------------------------------------
# Tab 3: Before you go (live)
# ---------------------------------------------------------------------------
with tab_live:
    st.caption("Use this the morning of the flight and again before you leave the house. It pulls the latest "
               "observations (METARs), TAFs, AIRMET/SIGMETs, pilot reports and radar, and re-runs the go/no-go "
               "with them. Then get your official briefing.")
    if valid_pair:
        plans, _ = plans_for(dep_id, dest_id)
        if not plans:
            st.warning("No route fits your fuel and leg limits.")
        else:
            lc1, lc2, lc3 = st.columns([3, 2, 2])
            with lc1:
                pi = st.selectbox("Route", range(len(plans)), format_func=lambda i: f"{plans[i].label} "
                                  f"({plans[i].total_nm:.0f} nm)", key="live_plan")
            with lc2:
                when_opt = st.selectbox("Leaving", ["Now", "In 30 minutes", "In 1 hour", "In 2 hours", "At…"],
                                        index=2, key="live_when")
            with lc3:
                at_time = st.time_input("Departure time", time(9, 0), step=900, key="live_time",
                                        disabled=when_opt != "At…")
            r1, r2 = st.columns([1, 3])
            with r1:
                if st.button("🔄 Refresh live data", width="stretch"):
                    get_awc.clear()
                    get_model_forecast_live.clear()
            with r2:
                auto = st.toggle("Auto-refresh every 5 minutes", False, key="live_auto")

            plan = plans[pi]

            @st.fragment(run_every=300 if auto else None)
            def live_panel():
                now = datetime.now(timezone.utc)
                offs = {"Now": 0, "In 30 minutes": 30, "In 1 hour": 60, "In 2 hours": 120}
                if when_opt == "At…":
                    dep_local = datetime.combine(datetime.now(ZONE).date(), at_time, ZONE)
                    if dep_local.astimezone(timezone.utc) < now - timedelta(minutes=5):
                        dep_local += timedelta(days=1)
                    dep_time = dep_local.astimezone(timezone.utc)
                else:
                    dep_time = now + timedelta(minutes=offs[when_opt] + 1)

                try:
                    fc = forecast_for([plan], live=True)
                except Exception as e:
                    st.error(f"Couldn't get the model forecast: {e}")
                    return
                if demo:
                    metars, tafs, advs, pireps, status, fetched = {}, {}, [], [], {}, now
                    st.info("Demo weather is on, so there's no live data. Remove ?demo=1 from the address.")
                else:
                    ids = tuple(a.ident for a in plan.airports)
                    pts = []
                    for leg in plan.legs:
                        mid = leg.samples[len(leg.samples) // 2]
                        near = min(db.airports.values(), key=lambda a: haversine_nm(a.lat, a.lon, mid[0], mid[1]))
                        pts.append((near.ident, max(60.0, leg.dist_nm / 2)))
                    pts += [(i, 50.0) for i in ids]
                    with st.spinner("Getting live weather…"):
                        metars, tafs, advs, pireps, status, fetched = get_awc(ids, tuple(pts))
                    for k, v in status.items():
                        note_source(k, v[0], f"{v[2]} items" if v[0] else v[3])

                ctx = Context(ac, mins, fc, db, tafs=tafs, advisories=advs, tz=TZ, now=now)
                r = evaluate_plan(ctx, plan, dep_time)

                st.markdown(f"#### If you leave at {ctx.local(dep_time):%-I:%M %p}: {STATUS_ICON[r.status]}")
                st.caption(f"Live data pulled {ctx.local(fetched):%-I:%M %p} ({ago(fetched)}). "
                           "METARs update hourly (more often when weather changes).")
                findings_block(r)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Arrive", ctx.local(r.arr_time).strftime("%-I:%M %p"))
                m2.metric("Flying time", f"{r.flight_hours:.1f} h")
                m3.metric("Cruise", " / ".join(f"{lr.alt_ft:,.0f}" for lr in r.legs))
                m4.metric("Land with", f"{min(lr.fuel_at_landing_gal for lr in r.legs):.1f} gal")

                st.markdown("##### Right now at your airports (METAR)")
                cols = st.columns(len(plan.airports))
                for col, ap in zip(cols, plan.airports):
                    with col:
                        m = metars.get(ap.ident)
                        if not m:
                            st.markdown(f"**{ap.ident}** — no report found")
                            continue
                        d = decode_metar(m)
                        borrowed = m.get("_station") != ap.ident
                        st.markdown(f"**{ap.ident}** {CAT_BADGE.get(d['category'], d['category'])}"
                                    + (f"  \n:gray[from {m['_station']}, {m['_dist_nm']:.0f} nm]" if borrowed else ""))
                        st.caption(f"Observed {ago(d['time'])}")
                        st.markdown(
                            f"- Wind **{d['wind_text']}**\n"
                            f"- Vis **{'10+' if (d['vis_sm'] or 0) >= 10 else d['vis_sm']} sm**\n"
                            f"- Clouds **{d['clouds'] or '—'}**\n"
                            f"- Temp/dew **{d['temp_c']}/{d['dew_c']} °C**"
                            + (f"\n- Altimeter **{d['altim_inhg']}**" if d['altim_inhg'] else "")
                            + (f"\n- Weather **{d['wx']}**" if d['wx'] else ""))
                        st.code(d["raw"], language=None, wrap_lines=True)

                st.markdown("##### Forecast at your times (TAF)")
                for lr in r.legs:
                    for ap, t_, wxr in ((lr.leg.dep, lr.dep_time, lr.dep_wx), (lr.leg.arr, lr.arr_time, lr.arr_wx)):
                        if lr.index > 0 and ap is lr.leg.dep:
                            continue
                        taf = tafs.get(ap.ident)
                        label = f"**{ap.ident}** at {ctx.local(t_):%-I:%M %p}: " + (wxr.summary() if wxr else "no data")
                        st.markdown(label)
                        if taf:
                            src = "" if taf.get("station") == ap.ident else \
                                f" (no TAF at {ap.ident}; nearest is {taf['station']}, {taf.get('dist_nm', 0):.0f} nm)"
                            with st.expander(f"Raw TAF{src}"):
                                st.code(taf["raw"], language=None, wrap_lines=True)

                st.markdown("##### Radar, advisories and pilot reports")
                route_paths = [[(la, lo) for la, lo, _ in leg.samples] for leg in plan.legs]
                ap_pts = []
                for ap in plan.airports:
                    m = metars.get(ap.ident)
                    d = decode_metar(m) if m else {}
                    ap_pts.append({"ident": ap.ident, "lat": ap.lat, "lon": ap.lon,
                                   "cat": d.get("category", ""), "raw": d.get("raw", "")})
                html_map = live_map_html(route_paths, ap_pts, advs, pireps, now)
                if hasattr(st, "iframe"):
                    st.iframe(html_map, height=560)
                else:
                    components.html(html_map, height=560)
                st.caption("Radar is the live NEXRAD composite (updates about every 5 minutes). Click shapes and "
                           "dots for details; use the box at top right to turn layers on or off.")

                on_route = []
                for a in advs:
                    if a.hazard in ("FZLVL", "TURB-HI"):
                        continue
                    for lr in r.legs:
                        hit = next(((la, lo, p.eta) for (la, lo, _), p in zip(lr.leg.samples, lr.profile)
                                    if a.covers(la, lo, p.eta)), None)
                        if hit:
                            on_route.append((a, lr.leg))
                            break
                if on_route:
                    st.markdown("**Advisories along your route at your ETA**")
                    st.dataframe(pd.DataFrame([{
                        "Type": a.kind, "Hazard": a.hazard, "Leg": f"{leg.dep.ident}-{leg.arr.ident}",
                        "Altitudes": ("SFC" if not a.base_ft else f"{a.base_ft:,.0f}") + "–" +
                                     ("" if a.top_ft is None else f"{a.top_ft:,.0f} ft"),
                        "Valid until": ctx.local(a.valid_to).strftime("%-I:%M %p") if a.valid_to else "",
                        "Details": a.raw[:200]} for a, leg in on_route]), hide_index=True, width="stretch")
                elif not demo:
                    st.success("No AIRMETs, SIGMETs or G-AIRMETs touch your route at your ETA.")

                near = []
                for p in pireps:
                    dmin = min(haversine_nm(p.lat, p.lon, la, lo) for leg in plan.legs for la, lo, _ in leg.samples)
                    if dmin <= 40:
                        near.append((p, dmin))
                if near:
                    st.markdown(f"**Pilot reports within 40 nm of your route (last 2 hours): {len(near)}**")
                    st.dataframe(pd.DataFrame([{
                        "Time": ctx.local(p.time).strftime("%-I:%M %p") if p.time else "",
                        "Altitude": "" if p.alt_ft is None else f"{p.alt_ft:,.0f}", "Aircraft": p.aircraft,
                        "Turbulence": p.turb, "Icing": p.icing, "Urgent": "⚠️" if p.urgent else "",
                        "Report": p.raw} for p, _ in sorted(near, key=lambda x: x[0].time or now, reverse=True)]),
                        hide_index=True, width="stretch")
                elif not demo:
                    st.caption("No pilot reports near your route in the last 2 hours.")

                st.markdown("**Official sources before you go:** "
                            "[1800wxbrief.com](https://www.1800wxbrief.com) · "
                            "[Graphical Forecasts (GFA)](https://aviationweather.gov/gfa/) · "
                            "[AWC radar & satellite](https://aviationweather.gov/) · "
                            "[TFRs](https://tfr.faa.gov/)")

            live_panel()


# ---------------------------------------------------------------------------
# Data status (sidebar) + footer
# ---------------------------------------------------------------------------
with status_box:
    srcs = st.session_state.get("sources", {})
    if srcs:
        st.caption("**Data sources (this session)**")
        for name, (ok, when, detail) in srcs.items():
            st.caption(f"{'✅' if ok else '❌'} {name} — {ago(when)}" + (f" · {detail}" if detail else ""))
    else:
        st.caption("Data sources will show here after the first lookup.")

st.divider()
st.caption(
    "**Screening tool, not a weather briefing.** Multi-day forecasts are model output (Open-Meteo/NOAA GFS, HRRR "
    "and others) and ceilings are estimated. Live observations, TAFs, AIRMET/SIGMETs and PIREPs come from "
    "aviationweather.gov; radar from the Iowa Environmental Mesonet. Before every flight get an official briefing "
    "([1800wxbrief.com](https://www.1800wxbrief.com), Garmin Pilot or ForeFlight), check NOTAMs/TFRs, confirm fuel "
    "at your stops on [AirNav](https://www.airnav.com), and use your POH for performance. "
    "[FAA Personal Minimums (PAVE)](https://www.faasafety.gov/files/gslac/courses/content/28/212/Personal%20Minimums%20Checklist.pdf)"
)
