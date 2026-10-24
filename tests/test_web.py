"""Web app tests: offline (synthetic weather, fake registry, captured email)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from flightplanner.demo import synthetic_forecast  # noqa: E402


@pytest.fixture
def web(tmp_path, fake_registry):
    from web.app import create_app
    sent = []

    def forecast(points):
        return synthetic_forecast(points, start=datetime.now(timezone.utc) - timedelta(hours=3), days=7)

    app = create_app(db_path=str(tmp_path / "web.sqlite3"), forecast=forecast, send_mail=sent.append,
                     base_url="http://test")
    client = TestClient(app, follow_redirects=True)
    client.sent = sent
    client.store = app.state.store
    return client


def _link(msg) -> str:
    body = msg.get_body(("plain",)).get_content()
    return re.search(r"http://test(/auth\?token=\S+)", body).group(1)


def _onboard(c, tail="N101SP", airport="KLEX", preset="standard", email="dad@example.com", **extra):
    r = c.post("/start/plane", data={"tail": tail})
    assert "1986 Cessna 182R" in r.text and "Yes, that's it" in r.text
    c.post("/start/plane", data={"tail": "N101SP", "type_id": "cessna-182/182R"})
    r = c.post("/start/airport", data={"q": airport})
    assert "Is this it?" in r.text
    c.post("/start/airport", data={"confirm": airport})
    c.post("/start/caution", data={"preset": preset})
    return c.post("/start/email", data={"email": email, "agree": "1", **extra})


def test_landing_and_terms(web):
    r = web.get("/")
    assert r.status_code == 200 and "Can you fly this weekend?" in r.text and "Screening only" in r.text
    assert "not an official weather briefing" in web.get("/terms").text


def test_onboarding_signs_in_and_shows_the_answer(web):
    r = _onboard(web)
    assert r.url.path == "/home"
    assert "Can you fly from KLEX?" in r.text and "Tomorrow" in r.text
    assert re.search(r"<span class=\"pill\">(GO|MARGINAL|NO-GO)</span>", r.text)
    assert "Cessna 182R Skylane" in r.text and "Standard" in r.text
    u = web.store.by_email("dad@example.com")
    assert u.aircraft == {"type": "cessna-182/182R", "tail_number": "N101SP"} and not u.verified
    assert len(web.sent) == 1 and web.sent[0]["To"] == "dad@example.com"
    # the emailed link confirms the address
    web.cookies.clear()
    r = web.get(_link(web.sent[0]))
    assert r.url.path == "/home" and web.store.by_email("dad@example.com").verified


def test_agreement_is_required(web):
    web.post("/start/plane", data={"skip": "1"})
    web.post("/start/airport", data={"confirm": "KLEX"})
    web.post("/start/caution", data={"preset": "cautious"})
    r = web.post("/start/email", data={"email": "x@example.com"})
    assert r.status_code == 400 and "screening tool" in r.text
    assert web.store.by_email("x@example.com") is None and not web.sent


def test_existing_email_is_not_overwritten(web):
    _onboard(web, preset="experienced")
    web.cookies.clear()
    web.sent.clear()
    r = _onboard(web, preset="cautious")
    assert "Check your email" in r.text
    assert web.store.by_email("dad@example.com").preset == "experienced"
    assert len(web.sent) == 1 and "already has an account" in web.sent[0].get_body(("plain",)).get_content()


def test_set_up_for_someone_else(web):
    r = _onboard(web, email="dad@example.com", for_someone="1", my_email="kid@example.com")
    assert "When they open it" in r.text
    u = web.store.by_email("dad@example.com")
    assert u.set_up_by == "kid@example.com"
    assert web.get("/home").url.path == "/login"               # the setter isn't signed in as Dad
    assert "kid@example.com set up" in web.sent[0].get_body(("plain",)).get_content()


def test_unknown_tail_and_skip(web):
    r = web.post("/start/plane", data={"tail": "N99999"})
    assert "find N99999 in the FAA registry" in r.text
    r = web.post("/start/plane", data={"tail": "N2RX"})          # an SR22: not in the catalog yet
    assert "isn't in our list yet" in r.text
    r = web.post("/start/plane", data={"skip": "1"})
    assert r.url.path == "/start/airport"


def test_login_never_reveals_accounts(web):
    a = web.post("/login", data={"email": "nobody@example.com"})
    assert "Check your email" in a.text and not web.sent
    _onboard(web)
    web.cookies.clear()
    web.sent.clear()
    b = web.post("/login", data={"email": "dad@example.com"})
    assert "Check your email" in b.text and len(web.sent) == 1
    assert web.get("/auth?token=garbage").status_code == 400


def test_settings_clamp_to_legal_floors_and_cap_crosswind(web):
    _onboard(web, preset="standard")
    web.post("/settings", data={"preset": "standard", "home_airport": "KGGE", "type_id": "cessna-182/182R",
                                "airport_min_ceiling_ft_agl": "300", "airport_min_vis_sm": "1",
                                "max_crosswind_kt": "20", "max_surface_wind_kt": "25", "max_gust_kt": "25"})
    from web.pilot import user_minimums
    u = web.store.by_email("dad@example.com")
    m = user_minimums(u)
    assert u.home_airport == "KGGE" and not u.weekly_email     # unticked box = off
    assert m.airport_min_ceiling_ft_agl == 1000 and m.airport_min_vis_sm == 3       # legal VFR floors
    assert m.max_crosswind_kt == 15                                                   # 182R demonstrated
    assert "Saved." in web.get("/settings?saved=1").text


def test_unsubscribe_and_weekly_recipients(web):
    from web.app import unsubscribe_url
    _onboard(web)
    u = web.store.by_email("dad@example.com")
    assert web.store.weekly_recipients() == []                  # not confirmed yet
    web.get(_link(web.sent[0]))
    assert [x.email for x in web.store.weekly_recipients()] == ["dad@example.com"]
    r = web.get(unsubscribe_url("http://test", u.id).replace("http://test", ""))
    assert "unsubscribed" in r.text and web.store.weekly_recipients() == []
    assert web.get("/unsubscribe?token=nope").status_code == 400


def test_thursday_email_from_the_web_database(web, tmp_path):
    import importlib.util
    from flightplanner.airports import AirportDB
    spec = importlib.util.spec_from_file_location("weekend_email", "scripts/weekend_email.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    _onboard(web, preset="cautious")
    _onboard(web, email="other@example.com")
    web.get(_link(web.sent[0]))                                  # only dad confirms
    subs = script.subscribers_from_db(str(tmp_path / "web.sqlite3"), "http://test")
    assert [s["email"] for s in subs] == ["dad@example.com"]
    assert subs[0]["mins"].max_crosswind_kt == 10                 # his Cautious preset, not the file defaults
    ap = AirportDB.load().get("KLEX")
    fc = synthetic_forecast([(ap.lat, ap.lon)], start=datetime.now(timezone.utc) - timedelta(hours=3), days=7)
    msg = script.build(subs[0], AirportDB.load(), fc)
    html = msg.get_body(("html",)).get_content()
    assert "Stop these emails" in html and "/unsubscribe?token=" in html
    assert msg["Subject"].startswith("Weekend flying at KLEX") and "crosswind 10 kt" in html
