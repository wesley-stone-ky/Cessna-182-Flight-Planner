"""User store. SQLite for now (one file, no server); the SQL is plain enough to move to Postgres.

One row per pilot: email, home airport, minimums (an onboarding preset plus any
fine-tuned numbers) and their airplane (a "my airplane" profile dict, see
flightplanner/aircraft.py).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    verified      INTEGER NOT NULL DEFAULT 0,   -- clicked a sign-in link at least once
    created_at    TEXT NOT NULL,
    agreed_at     TEXT NOT NULL,                -- ticked "screening tool, not a weather briefing"
    home_airport  TEXT NOT NULL,
    preset        TEXT NOT NULL DEFAULT 'cautious',
    minimums_json TEXT NOT NULL DEFAULT '{}',   -- fine-tuned numbers on top of the preset
    aircraft_json TEXT NOT NULL DEFAULT '{}',   -- {"type": ..., "tail_number": ..., <tune-ups>}
    weekly_email  INTEGER NOT NULL DEFAULT 1,
    set_up_by     TEXT NOT NULL DEFAULT ''      -- email of whoever set this up for them, if anyone
);
"""


@dataclass
class User:
    id: int
    email: str
    verified: bool
    home_airport: str
    preset: str
    minimums: dict = field(default_factory=dict)
    aircraft: dict = field(default_factory=dict)
    weekly_email: bool = True
    set_up_by: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _user(r: sqlite3.Row | None) -> User | None:
        if r is None:
            return None
        return User(r["id"], r["email"], bool(r["verified"]), r["home_airport"], r["preset"],
                    json.loads(r["minimums_json"]), json.loads(r["aircraft_json"]), bool(r["weekly_email"]),
                    r["set_up_by"])

    def get(self, user_id: int) -> User | None:
        with self._conn() as c:
            return self._user(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def by_email(self, email: str) -> User | None:
        with self._conn() as c:
            return self._user(c.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone())

    def create(self, email: str, home_airport: str, preset: str, aircraft: dict, set_up_by: str = "") -> User:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, created_at, agreed_at, home_airport, preset, aircraft_json, set_up_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email.lower(), _now(), _now(), home_airport, preset, json.dumps(aircraft), set_up_by.lower()))
            new_id = cur.lastrowid
        return self.get(new_id)     # after the commit, so the new connection can see the row

    def update(self, user_id: int, **fields) -> User:
        cols = {"verified": "verified", "home_airport": "home_airport", "preset": "preset",
                "minimums": "minimums_json", "aircraft": "aircraft_json", "weekly_email": "weekly_email"}
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{cols[k]} = ?")
            vals.append(json.dumps(v) if k in ("minimums", "aircraft") else (int(v) if isinstance(v, bool) else v))
        with self._conn() as c:
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*vals, user_id))
        return self.get(user_id)

    def weekly_recipients(self) -> list[User]:
        """Confirmed emails only: nobody gets the Thursday email until they've clicked a link."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM users WHERE verified = 1 AND weekly_email = 1 ORDER BY id").fetchall()
        return [self._user(r) for r in rows]
