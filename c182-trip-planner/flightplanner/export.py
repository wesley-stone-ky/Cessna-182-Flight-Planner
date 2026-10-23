"""Exports: Garmin .fpl (Garmin Pilot / ForeFlight import), GTN .gfp, navlog CSV, text brief."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .planner import Context, Leg, PlanResult


def leg_fpl(leg: Leg) -> str:
    """Garmin FlightPlan v1 XML for one leg (airport to airport, direct)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wps, rps = [], []
    for ap in (leg.dep, leg.arr):
        wps.append(
            "    <waypoint>\n"
            f"      <identifier>{escape(ap.ident)}</identifier>\n"
            "      <type>AIRPORT</type>\n"
            "      <country-code></country-code>\n"
            f"      <lat>{ap.lat:.6f}</lat>\n"
            f"      <lon>{ap.lon:.6f}</lon>\n"
            f"      <comment>{escape(ap.name)}</comment>\n"
            "    </waypoint>")
        rps.append(
            "    <route-point>\n"
            f"      <waypoint-identifier>{escape(ap.ident)}</waypoint-identifier>\n"
            "      <waypoint-type>AIRPORT</waypoint-type>\n"
            "      <waypoint-country-code></waypoint-country-code>\n"
            "    </route-point>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<flight-plan xmlns="http://www8.garmin.com/xmlschemas/FlightPlan/v1">\n'
            f"  <created>{now}</created>\n"
            "  <waypoint-table>\n" + "\n".join(wps) + "\n  </waypoint-table>\n"
            "  <route>\n"
            f"    <route-name>{leg.dep.ident} TO {leg.arr.ident}</route-name>\n"
            "    <flight-plan-index>1</flight-plan-index>\n" + "\n".join(rps) + "\n  </route>\n"
            "</flight-plan>\n")


def leg_gfp(leg: Leg) -> str:
    """GTN-series SD-card flight plan text (.gfp)."""
    return f"FPN/RI:F:{leg.dep.ident}:F:{leg.arr.ident}\n"


def navlog_csv(ctx: Context, r: PlanResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["leg", "from", "to", "dist_nm", "true_course", "mag_course", "mag_heading", "alt_ft",
                "avg_gs_kt", "wind_comp_kt(+tail)", "ete_min", "depart_local", "arrive_local",
                "fuel_used_gal", "fuel_at_landing_gal", "reserve_req_gal"])
    for lr in r.legs:
        L = lr.leg
        w.writerow([lr.index + 1, L.dep.ident, L.arr.ident, f"{L.dist_nm:.0f}", f"{L.true_course:03.0f}",
                    f"{L.mag_course:03.0f}", f"{lr.mag_heading:03.0f}", f"{lr.alt_ft:.0f}",
                    f"{lr.avg_gs_kt:.0f}", f"{lr.avg_wind_component_kt:+.0f}", f"{lr.minutes:.0f}",
                    ctx.local(lr.dep_time).strftime("%Y-%m-%d %H:%M"),
                    ctx.local(lr.arr_time).strftime("%Y-%m-%d %H:%M"),
                    f"{lr.fuel_used_gal:.1f}", f"{lr.fuel_at_landing_gal:.1f}", f"{lr.reserve_required_gal:.1f}"])
    return buf.getvalue()


def text_brief(ctx: Context, r: PlanResult) -> str:
    lines = [
        f"TRIP SCREEN — {r.plan.route_string}  ({r.plan.label})",
        f"Aircraft: {ctx.ac.name} @ {ctx.ac.cruise_setting} power, {ctx.ac.usable_gal:.0f} gal usable",
        f"Depart {ctx.local(r.dep_time):%a %b %d %-I:%M %p %Z}  ->  arrive {ctx.local(r.arr_time):%a %-I:%M %p %Z}",
        f"Status: {r.status}   Forecast confidence: {r.confidence}",
        f"Total {r.plan.total_nm:.0f} nm, {r.flight_hours:.1f} h flying, {r.elapsed_hours:.1f} h door to door",
        "",
    ]
    for lr in r.legs:
        L = lr.leg
        lines.append(f"Leg {lr.index + 1}: {L.dep.ident} -> {L.arr.ident}  {L.dist_nm:.0f} nm  "
                     f"MC {L.mag_course:03.0f}  MH {lr.mag_heading:03.0f}  {lr.alt_ft:,.0f} ft  "
                     f"GS {lr.avg_gs_kt:.0f} kt ({lr.avg_wind_component_kt:+.0f})  ETE {lr.minutes:.0f} min  "
                     f"fuel {lr.fuel_used_gal:.1f} gal, land with {lr.fuel_at_landing_gal:.1f}")
        if lr.dep_wx:
            lines.append(f"   {L.dep.ident} at departure: {lr.dep_wx.summary()}")
        if lr.arr_wx:
            lines.append(f"   {L.arr.ident} at arrival:   {lr.arr_wx.summary()}")
        lines.append(f"   Max terrain in corridor: {L.max_terrain():,.0f} ft")
    lines.append("")
    issues = [f for f in r.findings if f.level != "INFO"]
    if issues:
        lines.append("Items to look at:")
        for f in sorted(issues, key=lambda f: (f.level != "NO-GO", f.leg)):
            lines.append(f"  [{f.level}] leg {f.leg + 1}: {f.text}")
    else:
        lines.append("No issues found against your minimums.")
    lines += ["", "SCREENING ONLY. Get an official briefing (1800wxbrief.com, ForeFlight or Garmin Pilot),",
              "check NOTAMs/TFRs and fuel availability, and confirm performance with your POH before flight."]
    return "\n".join(lines)
