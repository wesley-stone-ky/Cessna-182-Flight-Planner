"""Send the "Can you fly this weekend?" email.

Run from the repo root:
    python scripts/weekend_email.py --to you@example.com --airport KLEX   # one test email
    python scripts/weekend_email.py --to you@example.com --dry-run        # write preview HTML, send nothing
    python scripts/weekend_email.py                                       # everyone on the list

Subscribers come from the SUBSCRIBERS_JSON environment variable (a GitHub
secret, so addresses never land in the repo) or config/subscribers.json
(git-ignored). See config/subscribers.example.json for the format.
With --from-db PATH they come from the web app's database instead (confirmed
emails only), each with its own minimums and an unsubscribe link; set
BASE_URL and the same SECRET_KEY as the web app so those links work.

Sending uses SMTP. For a Gmail sender: turn on 2-step verification, create an
App Password, and set SMTP_USER / SMTP_PASSWORD. SMTP_HOST, SMTP_PORT,
MAIL_FROM and MAIL_FROM_NAME are optional.

In GitHub Actions, TEST_TO / TEST_AIRPORT do the same as --to / --airport.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flightplanner.airports import AirportDB  # noqa: E402
from flightplanner.config import load_minimums  # noqa: E402
from flightplanner.mail import MailNotConfigured, send_messages  # noqa: E402
from flightplanner.weather import AviationWeather, fetch_forecast  # noqa: E402
from flightplanner.weekend import render_html, render_text, subject, weekend_for  # noqa: E402

def load_subscribers() -> list[dict]:
    raw = os.environ.get("SUBSCRIBERS_JSON")
    if not raw:
        path = ROOT / "config" / "subscribers.json"
        raw = path.read_text() if path.exists() else "[]"
    subs = json.loads(raw)
    for s in subs:
        if not s.get("email") or not s.get("airports"):
            raise ValueError(f"Each subscriber needs 'email' and 'airports': {s!r}")
    return subs


def subscribers_from_db(path: str, base_url: str) -> list[dict]:
    from web.auth import unsubscribe_url
    from web.db import Store
    from web.pilot import user_minimums
    return [{"email": u.email, "airports": [u.home_airport], "mins": user_minimums(u),
             "unsubscribe": unsubscribe_url(base_url, u.id)} for u in Store(path).weekly_recipients()]


def build(sub: dict, db: AirportDB, fc, tafs: dict | None = None) -> EmailMessage:
    mins = sub.get("mins") or load_minimums(sub.get("minimums"))
    weekends = []
    for i in sub["airports"]:
        ap = db.get(i)
        tz = sub.get("tz") or ap.tz      # each airport's own daylight hours unless the subscriber says otherwise
        weekends.append(weekend_for(fc, ap, mins, tz, datetime.now(ZoneInfo(tz)).date(), (tafs or {}).get(ap.ident)))
    msg = EmailMessage()
    msg["Subject"] = subject(weekends)
    msg["To"] = sub["email"]
    msg.set_content(render_text(weekends, mins, sub.get("unsubscribe")))
    msg.add_alternative(render_html(weekends, mins, sub.get("unsubscribe")), subtype="html")
    return msg


def send(msgs: list[EmailMessage]) -> None:
    try:
        send_messages(msgs)
    except MailNotConfigured as e:
        sys.exit(f"{e} (use --dry-run to preview instead).")
    for m in msgs:
        print(f"sent: {m['To']} — {m['Subject']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", default=os.environ.get("TEST_TO") or None, help="send one test email to this address")
    ap.add_argument("--airport", default=os.environ.get("TEST_AIRPORT") or "KLEX", help="airport for --to")
    ap.add_argument("--dry-run", action="store_true", help="write preview files instead of sending")
    ap.add_argument("--from-db", help="send to the web app's confirmed users in this SQLite file")
    ap.add_argument("--out", default=str(ROOT / ".cache" / "weekend_preview"), help="preview folder for --dry-run")
    args = ap.parse_args()

    if args.to:
        subs = [{"email": args.to, "airports": [args.airport]}]
    elif args.from_db:
        subs = subscribers_from_db(args.from_db, os.environ.get("BASE_URL", "http://localhost:8000"))
    else:
        subs = load_subscribers()
    if not subs:
        sys.exit("No subscribers: set SUBSCRIBERS_JSON, create config/subscribers.json, or pass --to.")
    db = AirportDB.load()
    aps = {i: db.get(i) for s in subs for i in s["airports"]}
    # One forecast fetch for every airport on the list. If NWS fails, send nothing:
    # an email built on missing data is worse than no email.
    fc, status = fetch_forecast([(a.lat, a.lon) for a in aps.values()])
    for name, st in status.items():
        print(f"{name}: {'ok' if st.ok else 'FAILED'} ({st.count} points){' — ' + st.error if st.error else ''}")
        if not st.ok:
            sys.exit("Forecast unavailable; not sending.")
    try:   # Friday's TAF is out by Thursday evening; it can only make an answer more cautious
        _, tafs = AviationWeather(ttl_s=600).for_airports(list(aps.values()), db.nearby)
    except Exception as e:
        print(f"TAFs unavailable ({e}); using the NWS grid only.")
        tafs = {}
    msgs = [build(s, db, fc, tafs) for s in subs]
    if args.dry_run:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for i, m in enumerate(msgs):
            html = m.get_body(("html",)).get_content()
            (out / f"{i}.html").write_text(html)
            print(f"preview: {out / f'{i}.html'} — {m['Subject']}")
        print(msgs[0].get_body(("plain",)).get_content())
        return
    send(msgs)


if __name__ == "__main__":
    main()
