"""Auth for gating the demo before it's shared with Waterpower — now with
real, server-side-killable sessions rather than just a signed cookie.

Design choice: a small, fixed set of named accounts via environment
variables (unchanged from before). What's new is how a *session* itself
is tracked: a signed cookie alone can't be revoked once issued — the
browser holds a valid, unforgeable token and there's nothing server-side
to invalidate short of rotating SESSION_SECRET, which kills every
session for every account at once. So every login now creates a row in
the `active_sessions` table (see models.py), and the cookie carries only
an opaque token pointing at it. That row is what actually gets checked,
aged out, or deleted — the cookie itself never needs to change.

Three accounts, each optional (skipped if its env vars aren't set):
    Admin    — WATERPOWER_USERNAME / WATERPOWER_PASSWORD (no credential
               expiry; also the only account allowed to trigger the
               kill-switch — see is_admin())
    Test     — TEST_USERNAME / TEST_PASSWORD (no expiry)
    Trial    — TRIAL_USERNAME / TRIAL_PASSWORD, expires after TRIAL_EXPIRES
               (an ISO date, e.g. "2026-09-01"). Meant for sharing with
               Waterpower for a time-boxed trial rather than indefinitely.

Session lifetime, both configurable via env vars:
    SESSION_IDLE_TIMEOUT_MINUTES (default 30) — a session with no request
        in this long is treated as logged out. Sliding: any request
        resets the clock, so it's genuinely "idle" time, not a hard cap.
    SESSION_ABSOLUTE_HOURS (default 12) — regardless of activity, a
        session older than this is force-expired. Bounds how long a
        stolen or forgotten-open session cookie stays useful.

Also set: SESSION_SECRET=<random 32+ char string>

Trade-off worth naming: validating against the session table costs one
DB lookup per request (a primary-key lookup, so cheap, but non-zero —
notably on Neon, whose network round-trip we spent real effort
minimizing elsewhere in this app). That cost is the actual price of a
session being genuinely revocable rather than merely locally trusted;
there's no way to keep both "checkable/killable at any time" and
"zero server involvement per request" simultaneously. last_seen_at is
only written when it's more than ~60s stale, to keep the write side of
that cost low even though the read is unavoidable.
"""
import hmac
import os
import secrets
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import ActiveSession

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-secret-change-before-deploying")
IDLE_TIMEOUT_MINUTES = float(os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", "30"))
ABSOLUTE_SESSION_HOURS = float(os.getenv("SESSION_ABSOLUTE_HOURS", "12"))
_LAST_SEEN_WRITE_GRANULARITY = timedelta(seconds=60)


def _parse_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _account(username_env: str, password_env: str, expires_env: str = None, default_username: str = None, default_password: str = None):
    """Builds one account entry from env vars, or None if not configured."""
    username = os.getenv(username_env, default_username)
    password = os.getenv(password_env, default_password)
    if not username or not password:
        return None
    return {
        "username": username,
        "password": password,
        "expires": _parse_date(os.getenv(expires_env)) if expires_env else None,
    }


# Keeps backward compatibility with the original single-account env vars
# (WATERPOWER_USERNAME/PASSWORD) as the admin account, so anything already
# configured in Render keeps working unchanged.
ACCOUNTS = [
    a for a in [
        _account("WATERPOWER_USERNAME", "WATERPOWER_PASSWORD", default_username="admin", default_password="waterpower2026"),
        _account("TEST_USERNAME", "TEST_PASSWORD"),
        _account("TRIAL_USERNAME", "TRIAL_PASSWORD", expires_env="TRIAL_EXPIRES"),
    ] if a is not None
]
ADMIN_USERNAME = os.getenv("WATERPOWER_USERNAME", "admin")

# Paths reachable without a session — the login page itself, its form
# submission, and the health check (useful for hosting-platform uptime pings).
PUBLIC_PATHS = {"/login", "/api/health"}
PUBLIC_PREFIXES = ("/login-assets/",)


def check_credentials(username: str, password: str) -> Tuple[bool, Optional[str]]:
    """Constant-time comparison to avoid leaking password length/prefix via
    response-timing side channels. Checks every configured account rather
    than short-circuiting on the first match, so timing doesn't reveal
    which username exists. Returns (ok, reason) — reason is "expired" if
    the credentials were otherwise correct but the account's trial window
    has passed, so the login page can show a clearer message than a flat
    "incorrect password"."""
    matched_but_expired = False
    ok = False
    for acct in ACCOUNTS:
        user_match = hmac.compare_digest(username, acct["username"])
        pass_match = hmac.compare_digest(password, acct["password"])
        if user_match and pass_match:
            if acct["expires"] and date.today() > acct["expires"]:
                matched_but_expired = True
            else:
                ok = True
    if ok:
        return True, None
    if matched_but_expired:
        return False, "expired"
    return False, None


def is_admin(username: Optional[str]) -> bool:
    return bool(username) and hmac.compare_digest(username, ADMIN_USERNAME)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def create_session(db, username: str) -> str:
    """Call on successful login. Kills any other active session for this
    same account first — "single active session per account" — so a
    shared trial credential can only be logged in from one place at a
    time; a fresh login elsewhere silently ends the old one rather than
    both staying valid side by side."""
    db.execute(delete(ActiveSession).where(ActiveSession.username == username))
    session_id = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db.add(ActiveSession(session_id=session_id, username=username, created_at=now, last_seen_at=now))
    db.commit()
    return session_id


def validate_session(db, session_id: Optional[str]) -> Optional[str]:
    """Returns the username if session_id points at a live, non-expired
    session — checking and enforcing both the idle timeout and the
    absolute lifetime — or None if it's missing, unknown, or expired
    (deleting the row in the expired case, so it doesn't linger).
    On success, refreshes last_seen_at (but only writes if it's gone
    stale by more than _LAST_SEEN_WRITE_GRANULARITY, to avoid a write on
    literally every single request)."""
    if not session_id:
        return None
    row = db.get(ActiveSession, session_id)
    if not row:
        return None

    now = datetime.utcnow()
    if now - row.last_seen_at > timedelta(minutes=IDLE_TIMEOUT_MINUTES):
        db.delete(row)
        db.commit()
        return None
    if now - row.created_at > timedelta(hours=ABSOLUTE_SESSION_HOURS):
        db.delete(row)
        db.commit()
        return None

    if now - row.last_seen_at > _LAST_SEEN_WRITE_GRANULARITY:
        row.last_seen_at = now
        db.commit()

    return row.username


def kill_session(db, session_id: str):
    db.execute(delete(ActiveSession).where(ActiveSession.session_id == session_id))
    db.commit()


def kill_all_sessions(db) -> int:
    """The manual kill-switch — forces every currently-logged-in session,
    across every account, to re-authenticate on their next request.
    Returns how many sessions were ended, mainly so the caller can
    confirm something actually happened."""
    result = db.execute(delete(ActiveSession))
    db.commit()
    return result.rowcount


async def auth_gate_middleware(request: Request, call_next):
    """Blocks every request except the login page/assets/health check
    unless the session cookie points at a live, non-expired,
    non-revoked session row."""
    if is_public_path(request.url.path):
        return await call_next(request)

    session_id = request.session.get("session_id")
    db = SessionLocal()
    try:
        username = validate_session(db, session_id)
    finally:
        db.close()

    if not username:
        request.session.clear()
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login")

    request.state.username = username
    return await call_next(request)
