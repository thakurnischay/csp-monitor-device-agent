"""Security primitives shared across the server: API-key hashing, CSRF
tokens, and a minimal in-process rate limiter.

Kept dependency-free (stdlib only) on purpose - this project's own
convention throughout (agent and server) is to avoid adding libraries
unless clearly justified, and none of this needs one.
"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from flask import session


# ---------------------------------------------------------------------------
# API keys
#
# API keys are high-entropy random tokens (32 bytes from secrets.token_urlsafe),
# not human-chosen passwords - unlike a password, they don't need a slow,
# salted KDF (bcrypt/pbkdf2) to resist brute force; a single fast SHA-256 over
# the whole token is standard practice for this (this is how GitHub/Stripe-style
# API keys are stored). Using a fast hash also matters here specifically
# because every single agent heartbeat (every few minutes, per CSP) re-checks
# this - a deliberately slow password hash would add needless CPU cost per
# request at fleet scale.
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def verify_api_key(key: str, stored_hash: str) -> bool:
    if not key or not stored_hash:
        return False
    return hmac.compare_digest(hash_api_key(key), stored_hash)


def key_suffix(key: str, length: int = 4) -> str:
    """The last few characters, captured at issue time so the admin UI can
    show a recognizable suffix without ever storing/re-reading the real key."""
    return (key or "")[-length:]


# ---------------------------------------------------------------------------
# CSRF - a minimal hand-rolled token, not a new dependency (flask-wtf would
# be the "normal" choice, but this app has exactly 3 POST forms and no other
# use for a form library, so a ~15-line token check is more proportionate).
# Only applies to the session-cookie-authenticated admin UI (the `ui`
# blueprint) - the agent-facing `/api/report` uses a header API key, not
# cookies, so it isn't a CSRF target and is deliberately left alone.
# ---------------------------------------------------------------------------

def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def verify_csrf(submitted_token: str) -> bool:
    expected = session.get("_csrf_token")
    return bool(expected) and bool(submitted_token) and hmac.compare_digest(expected, submitted_token)


# ---------------------------------------------------------------------------
# Rate limiting - a small in-memory sliding-window counter. Single-process
# only (fine for this deployment model: one Flask process per install, no
# multi-worker/multi-host setup) - if that ever changes, this would need to
# move to something shared (e.g. the database or Redis), not before.
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_events:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        """Clears all tracked hits. Production never needs this - it exists
        for tests, since login_limiter/report_limiter below are process-wide
        singletons that would otherwise leak state between test cases that
        happen to share an IP or csp_id (the Flask test client always uses
        the same fake remote address, for instance)."""
        self._hits.clear()


# Shared limiter instances used by routes.py / api.py.
login_limiter = RateLimiter(max_events=8, window_seconds=60)       # per IP
report_limiter = RateLimiter(max_events=6, window_seconds=60)      # per csp_id
