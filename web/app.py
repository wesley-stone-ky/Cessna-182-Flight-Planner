"""The multi-user web app.

Run locally (from the repo root):
    pip install -r web/requirements.txt
    SECRET_KEY=change-me uvicorn web.app:create_app --factory --reload
then open http://localhost:8000

Pages: landing, three-question onboarding (airplane, home airport, how
cautious), sign-in by emailed link (no passwords), "Can I fly?", settings,
unsubscribe, terms. Plain HTML forms, no JavaScript, large type.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from flightplanner.aircraft import catalog
from flightplanner.airports import AirportDB
from flightplanner.config import minimum_presets
from flightplanner.mail import MailNotConfigured, message, send_messages
from flightplanner.registry import lookup as registry_lookup
from flightplanner.weather import fetch_forecast, snap
from flightplanner.weekend import DISCLAIMER, GO, MARGINAL, NOGO, can_i_fly

from . import auth
from .auth import unsubscribe_url  # noqa: F401  (re-exported)
from .db import Store, User
from .pilot import TUNABLE, clamp_minimums, user_aircraft, user_minimums

HERE = Path(__file__).resolve().parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION, ONB = "session", "onb"
VERDICT_STYLE = {GO: "go", MARGINAL: "marginal", NOGO: "nogo"}

templates = Jinja2Templates(directory=str(HERE / "templates"))

_FC_CACHE: dict = {}


def nws_forecast(points: list[tuple[float, float]]):
    """NWS forecast for these points, shared by every user for 20 minutes."""
    key = tuple(sorted({snap(la, lo) for la, lo in points}))
    hit = _FC_CACHE.get(key)
    if hit and time.time() - hit[0] < 1200:
        return hit[1]
    fc, _ = fetch_forecast(points, ttl_s=1200)
    _FC_CACHE[key] = (time.time(), fc)
    return fc


def _send_or_print(m) -> None:
    """Send for real when SMTP is set up; otherwise (local development) print the email instead."""
    try:
        send_messages([m])
    except MailNotConfigured:
        print(f"\n--- email not configured; would send to {m['To']}: {m['Subject']}\n"
              f"{m.get_body(('plain',)).get_content()}---\n", flush=True)


def create_app(db_path: str | None = None, forecast=None, send_mail=None, base_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Weekend Flying", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    store = Store(db_path or os.environ.get("WEB_DB", ".cache/web.sqlite3"))
    airports = AirportDB.load()
    get_forecast = forecast or nws_forecast
    mail = send_mail or _send_or_print
    secure = (base_url or os.environ.get("BASE_URL", "")).startswith("https://")

    # -- helpers -----------------------------------------------------------
    def url(request: Request, path: str) -> str:
        root = base_url or os.environ.get("BASE_URL") or str(request.base_url).rstrip("/")
        return root.rstrip("/") + path

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"disclaimer": DISCLAIMER, **ctx}, status_code=status)

    def me(request: Request) -> User | None:
        uid = auth.loads(SESSION, request.cookies.get(SESSION), auth.SESSION_MAX_AGE)
        return store.get(uid) if uid else None

    def sign_in(resp: Response, u: User) -> Response:
        resp.set_cookie(SESSION, auth.dumps(SESSION, u.id), max_age=auth.SESSION_MAX_AGE, httponly=True,
                        samesite="lax", secure=secure)
        return resp

    def onb(request: Request) -> dict:
        return auth.loads(ONB, request.cookies.get(ONB), auth.ONBOARDING_MAX_AGE) or {}

    def go(path: str, state: dict | None = None) -> RedirectResponse:
        r = RedirectResponse(path, status_code=303)
        if state is not None:
            r.set_cookie(ONB, auth.dumps(ONB, state), max_age=auth.ONBOARDING_MAX_AGE, httponly=True,
                         samesite="lax", secure=secure)
        return r

    def send_link(request: Request, u: User, subject: str, intro: str) -> None:
        link = url(request, f"/auth?token={auth.dumps('login', u.id)}")
        text = (f"{intro}\n\nOpen this link to sign in (it works for 24 hours):\n{link}\n\n"
                "If you didn't ask for this, ignore this email.")
        html = (f'<p style="font-size:17px">{intro}</p><p><a href="{link}" style="display:inline-block;'
                'background:#13233a;color:#fff;padding:14px 22px;border-radius:6px;text-decoration:none;'
                'font-size:18px">Sign in</a></p><p style="color:#56657a">The link works for 24 hours. If you '
                "didn't ask for this, ignore this email.</p>")
        mail(message(u.email, subject, text, html))

    def type_choices() -> list[tuple[str, str]]:
        cat = catalog()
        return sorted(((t, cat[t].name) for t in cat), key=lambda x: x[1])

    # -- landing -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        if me(request):
            return RedirectResponse("/home", status_code=303)
        return page(request, "landing.html")

    # -- onboarding 1: the airplane ----------------------------------------
    @app.get("/start", response_class=HTMLResponse)
    def start(request: Request):
        return page(request, "start_plane.html", step=1, types=type_choices())

    @app.post("/start/plane", response_class=HTMLResponse)
    def start_plane(request: Request, tail: str = Form(""), type_id: str = Form(""), skip: str = Form("")):
        state = onb(request)
        if skip:
            state["plane"] = {}
            return go("/start/airport", state)
        if type_id:
            if type_id not in catalog():
                return page(request, "start_plane.html", 400, step=1, types=type_choices(),
                            error="Please pick a type from the list.")
            state["plane"] = {"type": type_id, "tail_number": tail.strip().upper()}
            return go("/start/airport", state)
        if not tail.strip():
            return page(request, "start_plane.html", 400, step=1, types=type_choices(),
                        error="Type your tail number, pick your airplane from the list, or skip this step.")
        reg = registry_lookup(tail)
        if reg is None:
            return page(request, "start_plane.html", step=1, types=type_choices(), tail=tail.strip().upper(),
                        error=f"We couldn't find {tail.strip().upper()} in the FAA registry (we only have piston "
                              "airplanes). Pick your airplane from the list instead.")
        return page(request, "start_plane_confirm.html", step=1, reg=reg, types=type_choices(),
                    match=catalog()[reg.type_id].name if reg.type_id else None)

    # -- onboarding 2: home airport ----------------------------------------
    @app.get("/start/airport", response_class=HTMLResponse)
    def start_airport_page(request: Request):
        return page(request, "start_airport.html", step=2)

    @app.post("/start/airport", response_class=HTMLResponse)
    def start_airport(request: Request, q: str = Form(""), confirm: str = Form("")):
        state = onb(request)
        if confirm:
            try:
                state["airport"] = airports.get(confirm).ident
            except KeyError:
                return page(request, "start_airport.html", 400, step=2, error="Please search again.")
            return go("/start/caution", state)
        q = q.strip()
        if not q:
            return page(request, "start_airport.html", 400, step=2, error="Type an airport code or a town name.")
        try:
            exact = [airports.get(q)]
        except KeyError:
            exact = []
        hits = exact or airports.search(q, limit=8)
        if not hits:
            return page(request, "start_airport.html", step=2, q=q,
                        error=f"No public airport with a paved runway matches “{q}”. Try the code (like KLEX) "
                              "or the town.")
        return page(request, "start_airport.html", step=2, q=q, hits=hits)

    # -- onboarding 3: how cautious ----------------------------------------
    @app.get("/start/caution", response_class=HTMLResponse)
    def start_caution_page(request: Request):
        if not onb(request).get("airport"):
            return RedirectResponse("/start/airport", status_code=303)
        return page(request, "start_caution.html", step=3, presets=minimum_presets())

    @app.post("/start/caution")
    def start_caution(request: Request, preset: str = Form("cautious")):
        state = onb(request)
        state["preset"] = preset if preset in minimum_presets() else "cautious"   # unsure = the cautious one
        return go("/start/email", state)

    # -- onboarding 4: email + agreement -----------------------------------
    @app.get("/start/email", response_class=HTMLResponse)
    def start_email_page(request: Request):
        if not onb(request).get("airport"):
            return RedirectResponse("/start/airport", status_code=303)
        return page(request, "start_email.html", step=4)

    @app.post("/start/email", response_class=HTMLResponse)
    def start_email(request: Request, email: str = Form(""), agree: str = Form(""), for_someone: str = Form(""),
                    my_email: str = Form("")):
        state = onb(request)
        if not state.get("airport"):
            return RedirectResponse("/start/airport", status_code=303)
        email, my_email = email.strip().lower(), my_email.strip().lower()
        err = None
        if not EMAIL_RE.match(email):
            err = "Please check the email address."
        elif not agree:
            err = "Please tick the box to confirm you understand this is a screening tool, not a weather briefing."
        elif for_someone and my_email and not EMAIL_RE.match(my_email):
            err = "Please check your own email address."
        if err:
            return page(request, "start_email.html", 400, step=4, error=err, email=email, for_someone=for_someone,
                        my_email=my_email)
        existing = store.by_email(email)
        if existing:
            # Never overwrite someone's settings from a sign-up form; just send them a sign-in link.
            send_link(request, existing, "Your sign-in link", "Someone (hopefully you) tried to sign up with this "
                      "email, which already has an account.")
            return page(request, "check_email.html", email=email)
        u = store.create(email, state["airport"], state.get("preset", "cautious"), state.get("plane") or {},
                         set_up_by=my_email if for_someone else "")
        if for_someone:
            who = my_email or "Someone"
            send_link(request, u, "Your weekend flying outlook is set up",
                      f"{who} set up a weekend flying outlook for you at {u.home_airport}. Open the link to see "
                      "this weekend's answer and to start the Thursday email.")
            r = page(request, "check_email.html", email=email, for_someone=True)
            r.delete_cookie(ONB)
            return r
        send_link(request, u, "Confirm your email", "Welcome! Confirm your email to start the Thursday "
                  "\"Can you fly this weekend?\" email.")
        r = go("/home?welcome=1")
        r.delete_cookie(ONB)
        return sign_in(r, u)

    # -- sign in / out -----------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return page(request, "login.html")

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, email: str = Form("")):
        u = store.by_email(email.strip().lower()) if EMAIL_RE.match(email.strip()) else None
        if u:
            send_link(request, u, "Your sign-in link", "Here's your sign-in link.")
        # Same answer either way, so the form doesn't reveal who has an account.
        return page(request, "check_email.html", email=email.strip().lower())

    @app.get("/auth")
    def auth_link(request: Request, token: str = ""):
        uid = auth.loads("login", token, auth.LOGIN_LINK_MAX_AGE)
        u = store.get(uid) if uid else None
        if u is None:
            return page(request, "message.html", 400, title="That link has expired",
                        body="Sign-in links work for 24 hours. Ask for a new one below.", link=("/login", "Sign in"))
        if not u.verified:
            u = store.update(u.id, verified=True)
        return sign_in(RedirectResponse("/home", status_code=303), u)

    @app.post("/logout")
    def logout():
        r = RedirectResponse("/", status_code=303)
        r.delete_cookie(SESSION)
        return r

    # -- the answer --------------------------------------------------------
    @app.get("/home", response_class=HTMLResponse)
    def home(request: Request, welcome: str = ""):
        u = me(request)
        if u is None:
            return RedirectResponse("/login", status_code=303)
        ap = airports.get(u.home_airport)
        mins = user_minimums(u)
        ac = user_aircraft(u)
        try:
            fc = get_forecast([(ap.lat, ap.lon)])
            answers = can_i_fly(fc, ap, mins, ap.tz, datetime.now(timezone.utc))
            error = None
        except Exception:
            answers, error = [], ("We couldn't reach the National Weather Service just now. Try again in a few "
                                  "minutes.")
        return page(request, "home.html", user=u, ap=ap, mins=mins, ac=ac, answers=answers, error=error,
                    style=VERDICT_STYLE, welcome=bool(welcome), preset=minimum_presets()[u.preset]["label"])

    # -- settings ----------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: str = ""):
        u = me(request)
        if u is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, "settings.html", user=u, mins=user_minimums(u), tunable=TUNABLE,
                    presets=minimum_presets(), types=type_choices(), ac=user_aircraft(u), saved=bool(saved))

    @app.post("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        u = me(request)
        if u is None:
            return RedirectResponse("/login", status_code=303)
        f = await request.form()
        changes: dict = {"weekly_email": bool(f.get("weekly_email"))}
        airport = str(f.get("home_airport", "")).strip()
        if airport:
            try:
                changes["home_airport"] = airports.get(airport).ident
            except KeyError:
                return page(request, "settings.html", 400, user=u, mins=user_minimums(u), tunable=TUNABLE,
                            presets=minimum_presets(), types=type_choices(), ac=user_aircraft(u),
                            error=f"We couldn't find the airport “{airport}”.")
        preset = str(f.get("preset", u.preset))
        if preset in minimum_presets():
            changes["preset"] = preset
        if preset != u.preset:
            changes["minimums"] = {}          # a new preset starts fresh
        else:
            changes["minimums"] = clamp_minimums({k: f.get(k) for k in TUNABLE})
        t = str(f.get("type_id", ""))
        plane = dict(u.aircraft)
        if t == "":
            plane = {}
        elif t in catalog():
            if t != plane.get("type"):
                plane = {"type": t, "tail_number": plane.get("tail_number", "")}
        changes["aircraft"] = plane
        store.update(u.id, **changes)
        return RedirectResponse("/settings?saved=1", status_code=303)

    # -- unsubscribe, legal ------------------------------------------------
    @app.get("/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(request: Request, token: str = ""):
        uid = auth.loads("unsub", token, None)
        u = store.get(uid) if uid else None
        if u is None:
            return page(request, "message.html", 400, title="That link didn't work",
                        body="Sign in and turn off the Thursday email in Settings.", link=("/login", "Sign in"))
        store.update(u.id, weekly_email=False)
        return page(request, "message.html", title="You're unsubscribed",
                    body=f"{u.email} won't get the Thursday email anymore. You can turn it back on in Settings.",
                    link=("/login", "Sign in"))

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return page(request, "terms.html")

    app.state.store = store
    return app


