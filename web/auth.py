"""Signed tokens: email sign-in links, the session cookie, unsubscribe links, onboarding state.

Everything is signed with SECRET_KEY (itsdangerous), so nothing needs a token table.
Set SECRET_KEY in production; without it a random key is used and links stop
working when the server restarts.
"""
from __future__ import annotations

import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

LOGIN_LINK_MAX_AGE = 24 * 3600          # sign-in links work for a day
SESSION_MAX_AGE = 90 * 24 * 3600        # stay signed in for 90 days
ONBOARDING_MAX_AGE = 24 * 3600

_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)


def _s(salt: str, key: str | None = None) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(key or _KEY, salt=salt)


def dumps(kind: str, data, key: str | None = None) -> str:
    return _s(kind, key).dumps(data)


def loads(kind: str, token: str | None, max_age: int | None, key: str | None = None):
    """Payload, or None if the token is missing, tampered with or expired."""
    if not token:
        return None
    try:
        return _s(kind, key).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def unsubscribe_url(base: str, user_id: int) -> str:
    """One-click unsubscribe link for the Thursday email (never expires)."""
    return base.rstrip("/") + f"/unsubscribe?token={dumps('unsub', user_id)}"
