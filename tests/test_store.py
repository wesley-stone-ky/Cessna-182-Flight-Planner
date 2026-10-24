"""User store contract, on SQLite always and on Postgres when TEST_DATABASE_URL is set."""
from __future__ import annotations

import os

import pytest

BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    from web.db import Store
    if request.param == "sqlite":
        return Store(tmp_path / "s.sqlite3")
    url = os.environ["TEST_DATABASE_URL"]
    import psycopg
    with psycopg.connect(url) as c:
        c.execute("DROP TABLE IF EXISTS users")
    return Store(url)


def test_create_read_update(store):
    u = store.create("Pilot@Example.com", "KLEX", "standard", {"type": "cessna-182/182R"}, set_up_by="Kid@x.com")
    assert u.id and u.email == "pilot@example.com" and u.set_up_by == "kid@x.com" and not u.verified
    assert store.by_email("PILOT@example.com").id == u.id
    v = store.update(u.id, verified=True, minimums={"max_crosswind_kt": 10}, aircraft={"type": "x"},
                     weekly_email=False, home_airport="KGGE", preset="cautious")
    assert (v.verified, v.minimums, v.aircraft, v.weekly_email, v.home_airport, v.preset) == \
        (True, {"max_crosswind_kt": 10}, {"type": "x"}, False, "KGGE", "cautious")
    assert store.get(999999) is None and store.by_email("nobody@x.com") is None


def test_emails_are_unique(store):
    store.create("a@x.com", "KLEX", "standard", {})
    with pytest.raises(Exception):
        store.create("A@x.com", "KGGE", "standard", {})


def test_weekly_recipients(store):
    a = store.create("a@x.com", "KLEX", "standard", {})
    b = store.create("b@x.com", "KLEX", "standard", {})
    store.create("c@x.com", "KLEX", "standard", {})            # never confirmed
    store.update(a.id, verified=True)
    store.update(b.id, verified=True, weekly_email=False)
    assert [u.email for u in store.weekly_recipients()] == ["a@x.com"]
