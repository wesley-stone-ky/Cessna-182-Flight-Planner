"""C182 Trip Planner — Streamlit UI.

Run locally:   streamlit run app.py
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from flightplanner.aircraft import Aircraft
from flightplanner.airports import FAVORITES, AirportDB
from flightplanner.config import load_minimums
from flightplanner.demo import synthetic_forecast
from flightplanner.export import leg_fpl, leg_gfp, navlog_csv, text_brief
from flightplanner.planner import (NOGO, CAUTION, Context, attach_terrain, candidate_plans,
                                   forecast_points, search_days)
from flightplanner.terrain import Terrain
from flightplanner.weather import AviationWeather, fetch_model_forecast

st.set_page_config(page_title="C182 Trip Planner", page_icon="✈️", layout="wide")

STATUS_ICON = {"GO": "🟢 GO", "MARGINAL": "🟡 MARGINAL", "NO-GO": "🔴 NO-GO"}
TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db() -> AirportDB:
    return AirportDB.load()


@st.cache_resource
def get_terrain() -> Terrain:
    return Terrain()


@st.cache_data(ttl=1800, show_spinner=False)
def get_model_forecast(points: tuple[tuple[float, float], ...]):
    return fetch_model_forecast(points)


@st.cache_data(ttl=600, show_spinner=False)
def get_awc(ids: tuple[str, ...]):
    awc = AviationWeather()
    return awc.metars(list(ids)), awc.tafs(list(ids)), awc.advisories(), awc.errors


# ---------------------------------------------------------------------------
# Sidebar: aircraft + minimums
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
                             help="182R standard tanks: 56 gal usable. Long-range: 88. Check your fuel selector placard.")
    with st.expander("Performance numbers in use"):
        cs = base_ac.raw["cruise_settings"][setting]
        st.write(f"Fuel flow: **{cs['gph']} gph**")
        st.dataframe(pd.DataFrame({"Altitude": list(cs["ktas"].keys()), "KTAS": list(cs["ktas"].values())}),
                     hide_index=True, width="stretch")
        st.caption("Edit `config/aircraft.yaml` with the numbers from YOUR POH, Section 5.")

    st.header("🧭 Personal minimums")
    rules = st.radio("Flight rules", ["VFR", "IFR"], horizontal=True,
                     index=0 if not base_mins.ifr else 1,
                     help="Use IFR only when you're instrument rated and current.")
    if rules == "IFR":
        st.warning("IFR mode screens for icing, convection and approach-level weather only. "
                   "It does not check approach minimums or alternates.")
    cig = st.slider("Airport ceiling (ft AGL)", 1000, 5000, int(base_mins.airport_min_ceiling_ft_agl), 500)
    vis = st.slider("Airport visibility (sm)", 3, 10, int(base_mins.airport_min_vis_sm))
    xw = st.slider("Max crosswind (kt)", 5, 15, int(base_mins.max_crosswind_kt))
    reserve = st.select_slider("Fuel reserve (min)", [30, 45, 60, 75, 90], int(base_mins.reserve_minutes))
    max_leg = st.slider("Longest leg you want to sit (h)", 1.5, 5.0, float(base_mins.max_leg_hours), 0.5)
    day_only = st.checkbox("Daylight only", base_mins.day_only)
    ground = st.slider("Time on the ground at a fuel stop (min)", 20, 120, int(base_mins.fuel_stop_ground_minutes), 5)

    st.header("🛰️ Data")
    demo = st.toggle("Demo weather (offline)", False,
                     help="Uses a made-up forecast with a cold front on day 3. For trying the app without internet.")

ac = Aircraft.load(cruise_setting=setting, overrides={"fuel.usable_gal": usable})
mins = load_minimums({
    "flight_rules": rules, "airport_min_ceiling_ft_agl": cig, "airport_min_vis_sm": vis,
    "max_crosswind_kt": xw, "reserve_minutes": reserve, "max_leg_hours": max_leg,
    "day_only": day_only, "fuel_stop_ground_minutes": ground,
})
db = get_db()

# ---------------------------------------------------------------------------
# Trip input
# ---------------------------------------------------------------------------
st.title("C182 Trip Planner")
st.caption("Pick your travel window. The planner tries every departure time and route option against the "
           "forecast and your minimums, and tells you which days work — nonstop if possible, fuel stops only "
           "when range or weather needs them.")

fav_ids = list(FAVORITES.keys())
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

today = datetime.now().date()
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
                                   min_value=today, max_value=today + timedelta(days=15), format="MM/DD/YYYY",
                                   key="retwin")
    with c6:
        ret_hours = st.slider("Return departure window", 5, 19, (9, 14), format="%d:00", key="rethrs")

go = st.button("Find the best days", type="primary", width="stretch")


def as_range(w) -> tuple[date, date]:
    if isinstance(w, (list, tuple)):
        return (w[0], w[-1]) if len(w) > 1 else (w[0], w[0])
    return w, w


def run_direction(dep_ident: str, dest_ident: str, win, hrs):
    dep, dest = db.get(dep_ident), db.get(dest_ident)
    plans = candidate_plans(db, dep, dest, ac, mins)
    if not plans:
        raise ValueError("No route works within your fuel and leg-length limits. "
                         "Try a longer max leg, smaller reserve, or check the usable fuel setting.")
    terrain = get_terrain()
    attach_terrain(plans, terrain, mins)
    pts = forecast_points(plans) + [(a.lat, a.lon) for p in plans for a in p.airports]
    now = datetime.now(timezone.utc)
    notes = []
    if demo:
        fc = synthetic_forecast(pts, start=now - timedelta(hours=1), days=16)
        metars, tafs, advs, errs = {}, {}, [], []
        notes.append("Demo weather — not a real forecast.")
    else:
        fc = get_model_forecast(tuple(sorted({(round(a, 3), round(b, 3)) for a, b in pts})))
        ids = tuple(sorted({a.ident for p in plans for a in p.airports}))
        metars, tafs, advs, errs = get_awc(ids)
        notes += [f"aviationweather.gov: {e}" for e in errs]
    if terrain.used_fallback:
        notes.append("Couldn't reach the terrain service; using conservative built-in terrain heights.")
    ctx = Context(ac, mins, fc, db, tafs=tafs, advisories=advs, tz=TZ, now=now)
    start, end = as_range(win)
    days = search_days(ctx, plans, start, end, hrs[0], hrs[1])
    return dict(ctx=ctx, plans=plans, days=days, metars=metars, tafs=tafs, notes=notes,
                dep=dep, dest=dest)


if go:
    try:
        with st.spinner("Pulling forecasts and checking every departure time…"):
            out = {"out": run_direction(dep_id, dest_id, window, hours)}
            if ret:
                out["back"] = run_direction(dest_id, dep_id, ret_window, ret_hours)
        st.session_state["results"] = out
    except KeyError as e:
        st.error(str(e).strip('"'))
    except Exception as e:  # show the problem instead of a stack trace
        st.error(f"Something went wrong: {e}")
        st.info("If the weather service is unreachable, switch on **Demo weather** in the sidebar to try the app.")


# ---------------------------------------------------------------------------
# Results
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
            "Day": d.day.strftime("%a %b %-d"),
            "Verdict": STATUS_ICON[b.status],
            "Leave": ctx.local(b.dep_time).strftime("%-I:%M %p"),
            "Route": b.plan.label,
            "Arrive": ctx.local(b.arr_time).strftime("%-I:%M %p"),
            "Flying": f"{b.flight_hours:.1f} h",
            "Confidence": b.confidence,
            "Why": " · ".join(b.top_reasons(2)) or "Meets your minimums",
        })
    return pd.DataFrame(rows)


def route_map(r):
    paths, pts = [], []
    for lr in r.legs:
        L = lr.leg
        paths.append({"path": [[lon, lat] for lat, lon, _ in L.samples], "name": f"{L.dep.ident}-{L.arr.ident}"})
    for a in r.plan.airports:
        pts.append({"lon": a.lon, "lat": a.lat, "ident": a.ident, "name": a.name})
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    view = pdk.ViewState(latitude=(min(lats) + max(lats)) / 2, longitude=(min(lons) + max(lons)) / 2,
                         zoom=5 if max(lats) - min(lats) < 6 else 4.4)
    return pdk.Deck(
        map_style=None, initial_view_state=view, tooltip={"text": "{ident}\n{name}"},
        layers=[
            pdk.Layer("PathLayer", paths, get_path="path", get_color=[37, 99, 235], width_min_pixels=3),
            pdk.Layer("ScatterplotLayer", pts, get_position=["lon", "lat"], get_radius=9000,
                      get_fill_color=[220, 38, 38], pickable=True),
            pdk.Layer("TextLayer", pts, get_position=["lon", "lat"], get_text="ident", get_size=14,
                      get_pixel_offset=[0, -18], get_color=[30, 30, 30]),
        ])


def profile_chart(r):
    rows = []
    offset = 0.0
    for lr in r.legs:
        for p in lr.profile:
            rows.append({"nm": offset + p.dist_nm, "Terrain (corridor max)": p.terrain_ft,
                         "Your altitude": p.alt_ft, "Est. cloud base": p.ceiling_msl_ft})
        offset += lr.leg.dist_nm
    df = pd.DataFrame(rows)
    base = alt.Chart(df).encode(x=alt.X("nm:Q", title="Distance (nm)"))
    terr = base.mark_area(color="#a8a29e", opacity=0.6).encode(y=alt.Y("Terrain (corridor max):Q", title="Feet MSL"))
    me = base.mark_line(color="#2563eb", strokeWidth=3).encode(y="Your altitude:Q")
    cl = base.mark_point(color="#64748b", filled=True, size=60, shape="triangle-down").encode(
        y="Est. cloud base:Q", tooltip=["nm", "Est. cloud base"])
    return (terr + me + cl).properties(height=260)


def show_direction(title: str, res: dict, key: str):
    ctx, days = res["ctx"], res["days"]
    st.subheader(title)
    for n in res["notes"]:
        st.caption(f"ℹ️ {n}")
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
                  key=f"{key}_opt", horizontal=False)
    r = opts[oi]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Verdict", STATUS_ICON[r.status])
    m2.metric("Leave", ctx.local(r.dep_time).strftime("%-I:%M %p"))
    m3.metric("Arrive", ctx.local(r.arr_time).strftime("%-I:%M %p"))
    m4.metric("Flying time", f"{r.flight_hours:.1f} h")
    m5.metric("Fuel stops", len(r.plan.stops))
    st.caption(f"Forecast confidence: **{r.confidence}** · {r.plan.total_nm:.0f} nm total")

    nogo = [f for f in r.findings if f.level == NOGO]
    caution = [f for f in r.findings if f.level == CAUTION]
    if nogo:
        st.error("**Deal-breakers**\n\n" + "\n".join(f"- Leg {f.leg + 1}: {f.text}" for f in nogo))
    if caution:
        st.warning("**Look closer**\n\n" + "\n".join(f"- Leg {f.leg + 1}: {f.text}" for f in caution))
    if not nogo and not caution:
        st.success("Nothing in the forecast breaks your minimums on this plan.")

    left, right = st.columns([3, 2])
    with left:
        st.pydeck_chart(route_map(r), width="stretch")
    with right:
        legs = []
        for lr in r.legs:
            L = lr.leg
            legs.append({
                "Leg": f"{L.dep.ident} → {L.arr.ident}", "Dist": f"{L.dist_nm:.0f} nm",
                "Alt": f"{lr.alt_ft:,.0f}", "MC/MH": f"{L.mag_course:03.0f}/{lr.mag_heading:03.0f}",
                "GS": f"{lr.avg_gs_kt:.0f} ({lr.avg_wind_component_kt:+.0f})", "ETE": f"{lr.minutes:.0f} min",
                "Fuel used": f"{lr.fuel_used_gal:.1f}", "Land with": f"{lr.fuel_at_landing_gal:.1f} gal",
            })
        st.dataframe(pd.DataFrame(legs), hide_index=True, width="stretch")
        for lr in r.legs:
            if lr.dep_wx:
                st.caption(f"**{lr.leg.dep.ident}** at {ctx.local(lr.dep_time):%-I:%M %p}: {lr.dep_wx.summary()}")
        last = r.legs[-1]
        if last.arr_wx:
            st.caption(f"**{last.leg.arr.ident}** at {ctx.local(last.arr_time):%-I:%M %p}: {last.arr_wx.summary()}")

    st.markdown("**Altitude profile** — blue is your planned altitude, grey is the highest terrain within "
                f"{mins.corridor_halfwidth_nm:.0f} nm of course, triangles are estimated cloud bases (MSL).")
    st.altair_chart(profile_chart(r), width="stretch")

    if d.by_hour:
        with st.expander("Every departure time that day"):
            st.dataframe(pd.DataFrame([{
                "Leave": ctx.local(h.dep_time).strftime("%-I:%M %p"), "Verdict": STATUS_ICON[h.status],
                "Route": h.plan.label, "Arrive": ctx.local(h.arr_time).strftime("%-I:%M %p"),
                "Why": " · ".join(h.top_reasons(1)) or "—"} for h in d.by_hour]),
                hide_index=True, width="stretch")

    ids = [a.ident for a in r.plan.airports]
    if res["metars"] or res["tafs"]:
        with st.expander("Current METARs / TAFs for these airports"):
            for i in ids:
                if i in res["metars"]:
                    st.code(res["metars"][i].get("rawOb", ""), language=None)
                if i in res["tafs"]:
                    st.code(res["tafs"][i]["raw"], language=None)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        for lr in r.legs:
            n = f"{lr.leg.dep.ident}-{lr.leg.arr.ident}"
            z.writestr(f"{n}.fpl", leg_fpl(lr.leg))
            z.writestr(f"{n}.gfp", leg_gfp(lr.leg))
    b1, b2, b3 = st.columns(3)
    stamp = ctx.local(r.dep_time).strftime("%Y%m%d")
    b1.download_button("📄 Trip summary (.txt)", text_brief(ctx, r), f"trip_{stamp}_{'-'.join(ids)}.txt",
                       width="stretch", key=f"{key}_txt")
    b2.download_button("🧾 Nav log (.csv)", navlog_csv(ctx, r), f"navlog_{stamp}_{'-'.join(ids)}.csv",
                       width="stretch", key=f"{key}_csv")
    b3.download_button("🛩️ Flight plans for Garmin (.zip)", zbuf.getvalue(), f"fpl_{stamp}_{'-'.join(ids)}.zip",
                       width="stretch", key=f"{key}_zip",
                       help=".fpl imports into Garmin Pilot / ForeFlight; .gfp goes on an SD card for the GTN.")


results = st.session_state.get("results")
if results:
    o = results["out"]
    show_direction(f"{o['dep'].ident} → {o['dest'].ident}", o, "out")
    if "back" in results:
        st.divider()
        b = results["back"]
        show_direction(f"Home: {b['dep'].ident} → {b['dest'].ident}", b, "back")

st.divider()
st.caption(
    "**Screening tool, not a weather briefing.** Multi-day forecasts are model output (Open-Meteo/NOAA GFS and others) "
    "and ceilings are estimated. Inside ~30 hours the app overlays TAFs and AIRMET/SIGMETs from aviationweather.gov. "
    "Before every flight get an official briefing ([1800wxbrief.com](https://www.1800wxbrief.com), Garmin Pilot or "
    "ForeFlight), check NOTAMs/TFRs, confirm fuel at your stops on [AirNav](https://www.airnav.com), and use your POH "
    "for performance. Useful: [Graphical Forecasts for Aviation](https://aviationweather.gov/gfa/) · "
    "[FAA Personal Minimums (PAVE)](https://www.faasafety.gov/files/gslac/courses/content/28/212/Personal%20Minimums%20Checklist.pdf)"
)
