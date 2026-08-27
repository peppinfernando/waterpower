"""Simple email/password auth for gating the demo before it's shared with
Waterpower.

Design choice: a single shared username/password pair via environment
variables, checked against a Starlette session cookie (signed with
itsdangerous, HttpOnly, not readable by JS). This is deliberately NOT a
full user-account system — for a proposal-stage demo that just needs to
keep anonymous visitors out, that would be over-engineering.

For anything beyond the proposal stage — real customer logins, per-user
permissions, password reset flows — swap this for a hosted auth provider
(Auth0, Supabase Auth, Clerk all have workable free tiers) rather than
extending this module. Hand-rolled auth is fine for "keep this demo
private," risky for "manage real customer accounts."

Credentials are read from env vars so they're never committed to the
repo. Set these before deploying:
    WATERPOWER_USERNAME=admin
    WATERPOWER_PASSWORD=<something not "waterpower2026">
    SESSION_SECRET=<random 32+ char string>
"""
import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

DEMO_USERNAME = os.getenv("WATERPOWER_USERNAME", "admin")
DEMO_PASSWORD = os.getenv("WATERPOWER_PASSWORD", "waterpower2026")  # CHANGE before deploying
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-secret-change-before-deploying")

# Paths reachable without a session — the login page itself, its form
# submission, and the health check (useful for hosting-platform uptime pings).
PUBLIC_PATHS = {"/login", "/api/health"}
PUBLIC_PREFIXES = ("/login-assets/",)


def check_credentials(username: str, password: str) -> bool:
    """Constant-time comparison to avoid leaking password length/prefix
    via response-timing side channels — cheap to do, no reason not to."""
    return (
        hmac.compare_digest(username, DEMO_USERNAME)
        and hmac.compare_digest(password, DEMO_PASSWORD)
    )


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
