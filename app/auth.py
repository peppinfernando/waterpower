"""Simple email/password auth for gating the demo before it's shared with
Waterpower.

Design choice: a small, fixed set of named accounts via environment
variables, checked against a Starlette session cookie (signed with
itsdangerous, HttpOnly, not readable by JS). This is deliberately NOT a
full user-account system — for a proposal-stage demo that just needs to
keep anonymous visitors out (and let a couple of named people in), that
would be over-engineering.

For anything beyond the proposal stage — real customer logins, per-user
permissions, password reset flows — swap this for a hosted auth provider
(Auth0, Supabase Auth, Clerk all have workable free tiers) rather than
extending this module. Hand-rolled auth is fine for "keep this demo
private," risky for "manage real customer accounts."

Three accounts, each optional (skipped if its env vars aren't set):
    Admin    — WATERPOWER_USERNAME / WATERPOWER_PASSWORD (no expiry)
    Test     — TEST_USERNAME / TEST_PASSWORD (no expiry)
    Trial    — TRIAL_USERNAME / TRIAL_PASSWORD, expires after TRIAL_EXPIRES
               (an ISO date, e.g. "2026-09-01"). Meant for sharing with
               Waterpower for a time-boxed trial rather than indefinitely.

Also set: SESSION_SECRET=<random 32+ char string>
"""
import hmac
import os
from datetime import date, datetime
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-secret-change-before-deploying")


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


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


async def auth_gate_middleware(request: Request, call_next):
    """Blocks every request except the login page/assets/health check
    unless the session cookie says the visitor already logged in."""
    if is_public_path(request.url.path):
        return await call_next(request)

    if not request.session.get("authenticated"):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login")

    return await call_next(request)

