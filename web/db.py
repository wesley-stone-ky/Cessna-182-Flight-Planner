"""User store: Postgres when DATABASE_URL is set (hosting), otherwise one SQLite file (local use).

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
    id            {id_col},
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
    """`where` is a SQLite file path or a postgres:// / postgresql:// URL."""

    def __init__(self, where: str | Path):
        self.where = str(where)
        self.pg = self.where.startswith(("postgres://", "postgresql://"))
        if not self.pg:
            Path(self.where).parent.mkdir(parents=True, exist_ok=True)
        schema = SCHEMA.replace("{id_col}", "BIGSERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY")
        with self._conn() as c:
            if self.pg:
                c.execute(schema)
            else:
                c.executescript(schema)

    def _conn(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            return psycopg.connect(self.where, row_factory=dict_row)
        c = sqlite3.connect(self.where)
        c.row_factory = sqlite3.Row
        return c

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    @staticmethod
    def _user(r: sqlite3.Row | None) -> User | None:
        if r is None:
            return None
        return User(r["id"], r["email"], bool(r["verified"]), r["home_airport"], r["preset"],
                    json.loads(r["minimums_json"]), json.loads(r["aircraft_json"]), bool(r["weekly_email"]),
                    r["set_up_by"])

    def get(self, user_id: int) -> User | None:
        with self._conn() as c:
            return self._user(c.execute(self._q("SELECT * FROM users WHERE id = ?"), (user_id,)).fetchone())

    def by_email(self, email: str) -> User | None:
        with self._conn() as c:
            return self._user(c.execute(self._q("SELECT * FROM users WHERE email = ?"), (email.lower(),)).fetchone())

    def create(self, email: str, home_airport: str, preset: str, aircraft: dict, set_up_by: str = "") -> User:
        with self._conn() as c:
            cur = c.execute(self._q(
                "INSERT INTO users (email, created_at, agreed_at, home_airport, preset, aircraft_json, set_up_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id"),
                (email.lower(), _now(), _now(), home_airport, preset, json.dumps(aircraft), set_up_by.lower()))
            new_id = cur.fetchone()["id"]
        return self.get(new_id)     # after the commit, so the new connection can see the row

    def update(self, user_id: int, **fields) -> User:
        cols = {"verified": "verified", "home_airport": "home_airport", "preset": "preset",
                "minimums": "minimums_json", "aircraft": "aircraft_json", "weekly_email": "weekly_email"}
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{cols[k]} = ?")
            vals.append(json.dumps(v) if k in ("minimums", "aircraft") else (int(v) if isinstance(v, bool) else v))
        with self._conn() as c:
            c.execute(self._q(f"UPDATE users SET {', '.join(sets)} WHERE id = ?"), (*vals, user_id))
        return self.get(user_id)

    def weekly_recipients(self) -> list[User]:
        """Confirmed emails only: nobody gets the Thursday email until they've clicked a link."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM users WHERE verified = 1 AND weekly_email = 1 ORDER BY id").fetchall()
        return [self._user(r) for r in rows]
