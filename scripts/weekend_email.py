"""Send the "Can you fly this weekend?" email.

Run from the repo root:
    python scripts/weekend_email.py --to you@example.com --airport KLEX   # one test email
    python scripts/weekend_email.py --to you@example.com --dry-run        # write preview HTML, send nothing
    python scripts/weekend_email.py                                       # everyone on the list

Subscribers come from the SUBSCRIBERS_JSON environment variable (a GitHub
secret, so addresses never land in the repo) or config/subscribers.json
(git-ignored). See config/subscribers.example.json for the format.

Sending uses SMTP. For a Gmail sender: turn on 2-step verification, create an
App Password, and set SMTP_USER / SMTP_PASSWORD. SMTP_HOST, SMTP_PORT,
MAIL_FROM and MAIL_FROM_NAME are optional.

In GitHub Actions, TEST_TO / TEST_AIRPORT do the same as --to / --airport.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flightplanner.airports import AirportDB  # noqa: E402
from flightplanner.config import load_minimums  # noqa: E402
from flightplanner.weather import fetch_forecast  # noqa: E402
from flightplanner.weekend import render_html, render_text, subject, weekend_for  # noqa: E402

DEFAULT_TZ = "America/New_York"


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


def build(sub: dict, db: AirportDB, fc) -> EmailMessage:
    tz = sub.get("tz", DEFAULT_TZ)
    mins = load_minimums(sub.get("minimums"))
    today = datetime.now(ZoneInfo(tz)).date()
    weekends = [weekend_for(fc, db.get(i), mins, tz, today) for i in sub["airports"]]
    msg = EmailMessage()
    msg["Subject"] = subject(weekends)
    msg["To"] = sub["email"]
    msg.set_content(render_text(weekends, mins))
    msg.add_alternative(render_html(weekends, mins), subtype="html")
    return msg


def send(msgs: list[EmailMessage]) -> None:
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not user or not pw:
        sys.exit("SMTP_USER and SMTP_PASSWORD must be set to send (use --dry-run to preview instead).")
    sender = formataddr((os.environ.get("MAIL_FROM_NAME") or "Weekend Flying Outlook",
                         os.environ.get("MAIL_FROM") or user))
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")),
                      timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        for m in msgs:
            m["From"] = sender
            s.send_message(m)
            print(f"sent: {m['To']} — {m['Subject']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", default=os.environ.get("TEST_TO") or None, help="send one test email to this address")
    ap.add_argument("--airport", default=os.environ.get("TEST_AIRPORT") or "KLEX", help="airport for --to")
    ap.add_argument("--dry-run", action="store_true", help="write preview files instead of sending")
    ap.add_argument("--out", default=str(ROOT / ".cache" / "weekend_preview"), help="preview folder for --dry-run")
    args = ap.parse_args()

    subs = [{"email": args.to, "airports": [args.airport]}] if args.to else load_subscribers()
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
    msgs = [build(s, db, fc) for s in subs]
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
