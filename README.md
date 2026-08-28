# Energy Cost Tracking & Billing Platform (Ireland I-SEM)

A backend platform that tracks wholesale electricity procurement cost based on
SEM-O Balancing Market (BM) settlement prices, aggregates it into daily /
weekly / monthly views, and provides a tariff calculator to help set
time-of-use retail pricing.

## Why it's built this way

- **SEM-O ingestion is an adapter, not a hard dependency.** The SEM-O site
  (`sem-o.com`) is a JavaScript single-page app; the real price data is
  served through their **Dynamic Reports** platform and a separate
  **Static Reports** file library, both of which normally require
  registering a SEM-O market-data account to get a report ID / API key.
  Rather than guess at an undocumented endpoint, `app/ingestion/sem_o_client.py`
  defines a small interface (`fetch_settlement_prices`) with two
  implementations:
    - `MockSemOClient` — deterministic synthetic BM prices, so you can build
      and demo the whole platform today.
    - `StaticReportSemOClient` — a stub that downloads and parses a SEM-O
      static CSV/XML report. Fill in the exact report URL/ID once you (or
      whoever holds the SEM-O portal login) confirm it — the rest of the
      system doesn't change.
  Swap which one is active with the `SEMO_CLIENT` env var.

- **Usage data is optional for v1.** You said usage data will come later, so
  the schema has a `usage_intervals` table ready to receive it (via CSV
  upload or API), but cost views work today using a configurable notional
  load (defaults to a flat 1 MW reference load) so you can see the *shape*
  of wholesale cost exposure immediately. Once real usage lands, cost
  becomes `price × actual_metered_volume` per interval automatically — no
  schema change needed.

- **Aggregate, not per-customer**, per your answer to Q3. The `cost_records`
  table is supplier-level. Per-customer allocation can be added later as a
  join on a `customer_usage` table without touching ingestion or pricing.

- **Periodic, not real-time**, per your answer to Q4. Ingestion runs on a
  schedule (every 30 min, matching BM settlement periods) via APScheduler,
  not a streaming pipeline. That keeps the stack simple: FastAPI + Postgres,
  no message broker needed.

## Stack

- **Python 3.11+, FastAPI** — REST API
- **SQLAlchemy 2.0 + Postgres** (SQLite fallback for local dev)
- **APScheduler** — periodic SEM-O polling
- **Pandas** — aggregation
- Plain HTML/JS dashboard in `frontend/` (swap for React later if needed)

## Project layout

```
app/
  main.py                 FastAPI app + startup scheduler
  database.py              DB engine/session
  models.py                 ORM models
  schemas.py                Pydantic response models
  ingestion/
    sem_o_client.py         Pluggable SEM-O adapter (mock + static-report stub)
  services/
    aggregation.py          Daily/weekly/monthly rollups
    tariff.py                Tariff/margin calculator
  routers/
    costs.py                 /api/costs/* endpoints
    tariff.py                 /api/tariff/* endpoints
frontend/
  index.html                Minimal dashboard (charts via Chart.js CDN)
```

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# uses SQLite by default; set DATABASE_URL for Postgres
export SEMO_CLIENT=mock   # or "static_report" once wired to a real SEM-O feed
export WATERPOWER_USERNAME=admin       # admin account
export WATERPOWER_PASSWORD=changeme     # change this — the code default is not secure
export TEST_USERNAME=test               # optional second account, e.g. for internal testing
export TEST_PASSWORD=changeme2
export TRIAL_USERNAME=waterpower        # optional third account, for a time-boxed trial share
export TRIAL_PASSWORD=changeme3
export TRIAL_EXPIRES=2026-09-01         # trial account stops working after this date (YYYY-MM-DD)
export SESSION_IDLE_TIMEOUT_MINUTES=30  # optional, defaults to 30 — auto-logout after this much inactivity
export SESSION_ABSOLUTE_HOURS=12        # optional, defaults to 12 — force re-login after this long regardless of activity
export SESSION_SECRET=$(openssl rand -hex 32)   # random secret for signing session cookies

python seed_mock_data.py --days 30   # or --days 1825 for 5 years, needed for year-over-year comparison
uvicorn app.main:app --reload
```

Then open **http://localhost:8000** in a browser — you'll land on a login
page first. Sign in with whatever you set `WATERPOWER_USERNAME` /
`WATERPOWER_PASSWORD` to (defaults to `admin` / `waterpower2026` if you
don't set them, but change that before showing this to anyone). Two
additional accounts are supported — `TEST_USERNAME`/`TEST_PASSWORD` and
`TRIAL_USERNAME`/`TRIAL_PASSWORD` — both optional (the login page just
won't accept them if their env vars aren't set). The trial account can
be given an expiry date via `TRIAL_EXPIRES`, useful for sharing
time-boxed access without needing to remember to revoke it manually.

**Session security:** sessions are now tracked server-side (not just a
signed cookie), which makes them genuinely revocable:
- **Auto-logout on inactivity** — `SESSION_IDLE_TIMEOUT_MINUTES` (default
  30). A sliding window: any request resets the clock.
- **Absolute session lifetime** — `SESSION_ABSOLUTE_HOURS` (default 12).
  Forces re-login after this long regardless of activity.
- **Single active session per account** — logging into an account again
  (e.g. from a different device) silently ends any other active session
  for that same account. Relevant if a shared credential like the trial
  login ever gets passed around more widely than intended.
- **Manual kill-switch** — the admin account only, from the Tools menu
  ("End all active sessions") or `POST /api/session/kill-all`. Ends
  every session for every account immediately, including the admin's
  own. Useful if you suspect a credential has leaked and want to force
  everyone to re-authenticate on demand rather than waiting for the
  trial expiry date.

The dashboard is now served *by* the FastAPI app itself (not opened as a
separate local file) — see `app/main.py`'s `StaticFiles` mount. This was a
deliberate change once auth was added: a same-origin setup means the
session cookie set at login is automatically sent with every API call, no
CORS credential juggling required, and it sidesteps some browser quirks
around `file://` pages (notably, Safari blocking `localStorage` for local
files) that came up during development.

You can still hit the API directly if useful:

- `GET /api/costs/daily?date_=2026-08-20`
- `GET /api/costs/weekly?week_start=2026-08-17`
- `GET /api/costs/monthly?year=2026&month=8`
- `GET /api/costs/yearly?year=2026`
- `GET /api/costs/range?start=2026-08-01&end=2026-08-08` — custom range, auto-picks granularity
- `GET /api/costs/compare?period=month&anchor=2026-08-01&years=5` — year-over-year comparison
- `POST /api/tariff/calculate` — turn a cost view into a proposed tariff

## Deploying somewhere Waterpower can actually see it

See **HOSTING.md** for a step-by-step guide (Render + Neon, both free,
verified against current 2026 pricing) plus a ready-to-use `render.yaml`
blueprint in this repo.

## Authentication — what this is and isn't

`app/auth.py` implements a single shared username/password pair, checked
against a signed session cookie. This is intentionally simple: it's meant
to keep an unshared proposal-stage demo private, not to manage real
customer accounts. Before this goes anywhere near production or handles
real customer data, replace it with a hosted auth provider — Auth0,
Supabase Auth, and Clerk all have workable free tiers and handle the
things this module deliberately doesn't (password reset, per-user
permissions, MFA, audit logs).

## Next steps to go from v1 to production

1. Register for a SEM-O market data account, get the real report ID/endpoint
   for **Imbalance Settlement Price** (30-min intervals), and fill in
   `StaticReportSemOClient`.
2. Add the real usage feed (file upload, SFTP drop, or API) into
   `usage_intervals` — the cost calculation will pick it up automatically.
3. Move from SQLite to Postgres (see HOSTING.md — needed anyway to deploy),
   add Alembic migrations.
4. Auth is in place for keeping the demo private (see above), but swap it
   for Auth0/Supabase/Clerk before this manages real customer accounts.
5. If per-customer costing is needed later, add a `customer_usage` table and
   a join in `services/aggregation.py`.
