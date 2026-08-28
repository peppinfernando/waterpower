import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.database import Base, engine, session_scope, get_db
from app.ingestion.sem_o_client import get_client
from app.models import SettlementPrice
from app.routers import costs, tariff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("energy-cost-platform")

scheduler = BackgroundScheduler()


def poll_sem_o():
    """Fetches the last few settlement periods from SEM-O and upserts them.
    Runs every 30 min to match the BM settlement period length. A short
    lookback window (last 3 hours) covers late-published/revised prices
    without re-fetching all of history each run."""
    client = get_client()
    end = datetime.utcnow()
    start = end - timedelta(hours=3)
    try:
        points = client.fetch_settlement_prices(start, end)
    except Exception:
        logger.exception("SEM-O ingestion failed")
        return

    with session_scope() as db:
        for p in points:
            existing = db.scalar(
                select(SettlementPrice).where(
                    SettlementPrice.interval_start == p.interval_start,
                    SettlementPrice.interval_minutes == p.interval_minutes,
                )
            )
            if existing:
                existing.price_eur_per_mwh = p.price_eur_per_mwh
                existing.net_imbalance_volume_mwh = p.net_imbalance_volume_mwh
            else:
                db.add(SettlementPrice(
                    interval_start=p.interval_start,
                    interval_minutes=p.interval_minutes,
                    price_eur_per_mwh=p.price_eur_per_mwh,
                    net_imbalance_volume_mwh=p.net_imbalance_volume_mwh,
                ))
    logger.info("Ingested %d settlement price points", len(points))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    poll_sem_o()  # populate on startup so the API isn't empty
    scheduler.add_job(poll_sem_o, "interval", minutes=30, id="semo_poll")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Energy Cost Tracking & Billing Platform", lifespan=lifespan)

# Session cookie signing — must run BEFORE auth_gate_middleware reads
# request.session. Starlette applies middleware in reverse-of-registration
# order (last added = outermost = runs first), so SessionMiddleware is
# added LAST here even though it must execute FIRST at request time.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # frontend is now same-origin (served below), so this mainly matters for direct API testing
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.middleware("http")(auth.auth_gate_middleware)

app.add_middleware(SessionMiddleware, secret_key=auth.SESSION_SECRET, same_site="lax")

app.include_router(costs.router)
app.include_router(tariff.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/login")
def login_page():
    return _serve_static_file("login.html")


@app.post("/login")
async def login_submit(request: Request):
    return await _handle_login(request)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    session_id = request.session.get("session_id")
    if session_id:
        auth.kill_session(db, session_id)  # server-side revoke, not just clearing the cookie
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/api/session/whoami")
def whoami(request: Request):
    username = getattr(request.state, "username", None)
    return {"username": username, "is_admin": auth.is_admin(username)}


@app.post("/api/session/kill-all")
def kill_all_sessions(request: Request, db: Session = Depends(get_db)):
    """The manual kill-switch. Deliberately restricted to the admin
    account — anyone could otherwise force-logout the trial account
    they're trying to demo to, which is the opposite of what this is
    for. Ends every session including the caller's own, which is the
    honest behavior for "force everyone to re-login," not a bug."""
    username = getattr(request.state, "username", None)
    if not auth.is_admin(username):
        raise HTTPException(403, "Only the admin account can do this")
    count = auth.kill_all_sessions(db)
    request.session.clear()
    return {"sessions_ended": count}


def _serve_static_file(name: str):
    from fastapi.responses import FileResponse
    path = os.path.join(os.path.dirname(__file__), "..", "frontend", name)
    return FileResponse(path)


async def _handle_login(request: Request):
    form = await request.form()
    username, password = form.get("username", ""), form.get("password", "")
    ok, reason = auth.check_credentials(username, password)
    if ok:
        with session_scope() as db:
            session_id = auth.create_session(db, username)
        request.session["session_id"] = session_id
        return RedirectResponse("/", status_code=303)
    error_code = "expired" if reason == "expired" else "1"
    return RedirectResponse(f"/login?error={error_code}", status_code=303)


# Serves the dashboard itself (frontend/index.html and its assets) from the
# same FastAPI app/origin as the API. This is deliberate, not incidental:
# same-origin means the session cookie set at /login is automatically sent
# with every fetch() the dashboard makes, no CORS credential dance needed,
# and it sidesteps the file:// quirks (opaque-origin localStorage errors,
# CORS preflight oddities) that came up when the dashboard was opened as a
# local file during development. Mounted last so it doesn't shadow the
# /api/*, /login, /logout routes above.
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "frontend"), html=True), name="frontend")
