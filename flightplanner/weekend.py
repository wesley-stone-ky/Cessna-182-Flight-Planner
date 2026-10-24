"""Can I fly? Today / tomorrow / weekend answers, and the weekend email.

Builds on outlook.airport_outlook (NWS 7-day grid, checked hour by hour against
the pilot's airport minimums) and turns each weekend day into GO / MARGINAL /
NO-GO with the best time window and the main reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from .airports import Airport
from .config import Minimums
from .outlook import DayOutlook, _windows, airport_outlook, explain_hours
from .weather import GridForecast

GO, MARGINAL, NOGO, NODATA = "GO", "MARGINAL", "NO-GO", "NO DATA"
VERDICT = {"Good": GO, "Fair": MARGINAL, "Partial": MARGINAL, "Poor": NOGO, "No data": NODATA}
PILL = {GO: ("#1d7a3e", "#e2f2e7"), MARGINAL: ("#1b5fae", "#e2ecf8"), NOGO: ("#b42b2b", "#f7e3e3"),
        NODATA: ("#56657a", "#e9edf2")}
DISCLAIMER = ("Screening only, not a weather briefing. Get an official briefing (1800wxbrief.com, ForeFlight or "
              "Garmin Pilot), check NOTAMs and TFRs, and use your POH before every flight.")


@dataclass
class WeekendDay:
    day: date
    verdict: str        # GO / MARGINAL / NO-GO / NO DATA
    window: str         # best stretch of flyable daylight hours, e.g. "9 AM–2 PM", or "—"
    reason: str


@dataclass
class AirportWeekend:
    airport: Airport
    days: list[WeekendDay]


def weekend_days(today: date) -> list[date]:
    """The weekend days still ahead: Fri-Sun from Mon-Thu, the rest of this weekend from Fri/Sat."""
    out, d = [], today + timedelta(days=1)
    while len(out) < 3:
        if d.weekday() >= 4:
            out.append(d)
        elif out:
            break
        d += timedelta(days=1)
    return out


def _reason(o: DayOutlook) -> str:
    if o.verdict == "No data":
        return "Past the end of the 7-day forecast."
    r = (o.reasons or [""])[0].split(" — ")[0]
    if o.verdict == "Good" and o.summary != "no significant weather":
        r += f" Watch for: {o.summary}."
    return r


def weekend_for(fc: GridForecast, ap: Airport, mins: Minimums, tz: str, today: date) -> AirportWeekend:
    want = weekend_days(today)
    outs = {o.day: o for o in airport_outlook(fc, ap, mins, tz, days=(want[-1] - today).days + 1, start=today)}
    days = []
    for d in want:
        o = outs.get(d)
        if o is None:
            days.append(WeekendDay(d, NODATA, "—", "Past the end of the 7-day forecast."))
            continue
        days.append(WeekendDay(d, VERDICT.get(o.verdict, NODATA), o.best_window, _reason(o)))
    return AirportWeekend(ap, days)


def ask_days(today: date) -> list[date]:
    """Today, tomorrow, and the rest of the coming weekend (within the 7-day forecast)."""
    days = [today, today + timedelta(days=1)]
    days += [d for d in weekend_days(today) if d not in days and (d - today).days <= 6]
    return days


def _rest_of_today(o: DayOutlook, now: datetime, mins: Minimums) -> WeekendDay:
    """Judge only the daylight hours still ahead."""
    hrs = [h for h in o.hours if h.time + timedelta(minutes=30) >= now]
    if len(hrs) < 2:
        return WeekendDay(o.day, NODATA, "—", "Not enough daylight left today.")
    ok = sum(h.ok for h in hrs)
    frac = ok / len(hrs)
    verdict = GO if frac >= 0.6 and ok >= min(3, len(hrs)) else MARGINAL if frac >= 0.3 else NOGO
    problems = explain_hours(hrs, len(hrs), mins)
    reason = problems[0].split(" — ")[0] if problems else "Every daylight hour left today meets your airport minimums."
    return WeekendDay(o.day, verdict, _windows([(h.time, h.ok) for h in hrs]), reason)


def can_i_fly(fc: GridForecast, ap: Airport, mins: Minimums, tz: str, now: datetime) -> list[tuple[str, WeekendDay]]:
    """[(label, answer)] for today, tomorrow and the weekend at one airport."""
    today = now.astimezone(ZoneInfo(tz)).date()
    want = ask_days(today)
    outs = {o.day: o for o in airport_outlook(fc, ap, mins, tz, days=(want[-1] - today).days + 1, start=today)}
    res = []
    for d in want:
        label = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else f"{d:%A}"
        o = outs.get(d)
        if o is None:
            res.append((label, WeekendDay(d, NODATA, "—", "Past the end of the 7-day forecast.")))
        elif d == today:
            res.append((label, _rest_of_today(o, now, mins)))
        else:
            res.append((label, WeekendDay(d, VERDICT.get(o.verdict, NODATA), o.best_window, _reason(o))))
    return res


def subject(weekends: list[AirportWeekend]) -> str:
    w = weekends[0]
    days = ", ".join(f"{d.day:%a} {d.verdict}" for d in w.days)
    more = f" (+{len(weekends) - 1} more)" if len(weekends) > 1 else ""
    return f"Weekend flying at {w.airport.ident}: {days}{more}"


def _mins_line(mins: Minimums) -> str:
    return (f"Your minimums: ceiling {mins.airport_min_ceiling_ft_agl:,.0f} ft, visibility "
            f"{mins.airport_min_vis_sm:.0f} sm, crosswind {mins.max_crosswind_kt:.0f} kt incl. gusts, wind "
            f"{mins.max_surface_wind_kt:.0f} kt, gusts {mins.max_gust_kt:.0f} kt, daylight only.")


def render_text(weekends: list[AirportWeekend], mins: Minimums, unsubscribe_url: str | None = None) -> str:
    lines = ["Can you fly this weekend?", ""]
    for w in weekends:
        lines.append(f"{w.airport.ident} ({w.airport.name})")
        for d in w.days:
            win = f"  best {d.window}" if d.window != "—" else ""
            lines.append(f"  {d.day:%a %b %-d}: {d.verdict}{win}")
            lines.append(f"    {d.reason}")
        lines.append("")
    lines += [_mins_line(mins), "Forecast: National Weather Service 7-day grid, checked hour by hour.", "",
              DISCLAIMER, "",
              f"To stop these emails: {unsubscribe_url}" if unsubscribe_url else "To stop these emails, reply STOP."]
    return "\n".join(lines)


def render_html(weekends: list[AirportWeekend], mins: Minimums, unsubscribe_url: str | None = None) -> str:
    """Email-safe HTML: tables and inline styles only."""
    font = "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
    rows = []
    for w in weekends:
        rows.append(f'<tr><td colspan="3" style="{font}padding:20px 0 6px;font-size:18px;font-weight:700;'
                    f'color:#13233a">{escape(w.airport.ident)} <span style="font-weight:400;color:#56657a;'
                    f'font-size:14px">{escape(w.airport.name)}</span></td></tr>')
        for d in w.days:
            fg, bg = PILL[d.verdict]
            rows.append(
                f'<tr><td style="{font}padding:10px 12px 10px 0;border-top:1px solid #d6dde5;vertical-align:top;'
                f'width:90px;font-size:15px;font-weight:600;color:#13233a">{d.day:%a %b %-d}</td>'
                f'<td style="{font}padding:10px 12px 10px 0;border-top:1px solid #d6dde5;vertical-align:top;'
                f'width:110px"><span style="display:inline-block;padding:4px 8px;border-radius:4px;'
                f'background:{bg};color:{fg};font-size:12px;font-weight:700;letter-spacing:.5px">'
                f'{d.verdict}</span></td>'
                f'<td style="{font}padding:10px 0;border-top:1px solid #d6dde5;vertical-align:top;font-size:14px;'
                f'color:#13233a">' + (f'<b>Best {escape(d.window)}</b><br>' if d.window != "—" else "")
                + f'<span style="color:#56657a">{escape(d.reason)}</span></td></tr>')
    return (
        f'<div style="background:#f4f6f7;padding:24px 12px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;'
        f'margin:0 auto;background:#ffffff;border:1px solid #d6dde5;border-radius:8px">'
        f'<tr><td style="padding:24px">'
        f'<div style="{font}font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#9b1f63">'
        f'Weekend flying outlook</div>'
        f'<div style="{font}font-size:24px;font-weight:800;color:#13233a;padding-top:6px">'
        f'Can you fly this weekend?</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%">{"".join(rows)}</table>'
        f'<p style="{font}font-size:13px;color:#56657a;margin:20px 0 0">{escape(_mins_line(mins))} '
        f'Forecast: National Weather Service 7-day grid, checked hour by hour.</p>'
        f'<p style="{font}font-size:13px;color:#13233a;margin:12px 0 0;padding:10px 12px;background:#f6e4ee;'
        f'border-radius:4px">{escape(DISCLAIMER)}</p>'
        f'<p style="{font}font-size:12px;color:#8794a6;margin:16px 0 0">' + (
            f'<a href="{escape(unsubscribe_url)}" style="color:#8794a6">Stop these emails</a>' if unsubscribe_url
            else "To stop these emails, reply STOP.") + '</p>'
        f'</td></tr></table></div>')
