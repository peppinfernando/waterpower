# Hosting this for the Waterpower demo

Recommendation, checked against current (August 2026) pricing: **Render**
(free web service) **+ Neon** (free Postgres). Both genuinely free, no
credit card, no trial clock. This isn't the only option — alternatives and
trade-offs are at the bottom — but it's the one I'd start with for a
proposal-stage demo.

## Why this combination specifically

- **Render's free web service** costs €0, deploys straight from a GitHub
  repo, and gives you a live HTTPS URL (`https://your-app.onrender.com`)
  with zero config. The catch: it spins down after 15 minutes with no
  traffic and takes about a minute to wake back up on the next request.
  For a proposal you're sharing occasionally, that's a fair trade for
  free — just warn whoever you send the link that the first load might be
  slow, or open the link yourself a minute before a call to "wake" it.

- **Render's own free Postgres is not the right database for this** — it
  auto-deletes 30 days after creation. Fine for a throwaway test, wrong
  for something you want live for an ongoing proposal.

- **Neon's free Postgres has no such expiry.** It's a genuinely permanent
  free tier: 0.5 GB storage, 100 compute-hours/month, no card required.
  This app's data footprint is tiny even with 5 years of mock settlement
  prices (well under 10 MB), so you won't come close to the limit. Neon
  also scales its compute to zero when idle, which pairs naturally with
  Render's own spin-down behaviour — nothing is costing money while
  nobody's looking at the dashboard.

## Steps

1. **Push this project to a GitHub repo** (private is fine — Render can
   deploy from private repos once you connect your GitHub account).

2. **Create a free Neon project** at neon.tech (no card needed). Copy the
   connection string it gives you — it looks like:
   ```
   postgresql://user:password@ep-xxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
   SQLAlchemy needs the `postgresql+psycopg2://` prefix instead of
   `postgresql://` — swap that part when you paste it in step 4.

3. **Create a free Render account**, connect your GitHub repo, and either:
   - Click "New +" → "Blueprint" and point it at this repo — Render will
     read `render.yaml` (already in this project) and set most things up
     automatically, or
   - Create a Web Service manually with:
     - Runtime: Python
     - Build command: `pip install -r requirements.txt`
     - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
     - Health check path: `/api/health`

4. **Set environment variables** in the Render dashboard (Settings →
   Environment):
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | your Neon string, with `postgresql+psycopg2://` prefix |
   | `WATERPOWER_USERNAME` | whatever login you want for the demo |
   | `WATERPOWER_PASSWORD` | a real password — not the `waterpower2026` placeholder in the code |
   | `SESSION_SECRET` | a random 32+ character string (Render can generate this for you if using the Blueprint) |

5. **Deploy.** Render builds and gives you a URL. First load will be slow
   (cold start on both Render and Neon) — that's expected, not a bug.

6. **Seed the database once, remotely.** The seed script needs to run
   against the *deployed* database, not your laptop's local one. Easiest
   way: open a Render Shell (Dashboard → your service → Shell tab) and run
   `python seed_mock_data.py --days 1825` there — it'll use the same
   `DATABASE_URL` the live app uses.

## Before you actually send the link to Waterpower

- Change `WATERPOWER_PASSWORD` away from the placeholder — the code
  defaults to `waterpower2026` if the env var isn't set, which is not a
  real password and shouldn't be relied on for anything beyond local dev.
- Consider a custom subdomain later (e.g. `dashboard.waterpower.ie`) —
  Render supports custom domains free on every plan, Waterpower would just
  need to add a CNAME record pointing at your `.onrender.com` URL.
- The free tier's cold-start delay is fine for an emailed link someone
  opens on their own time, but awkward mid-presentation. If you're
  demoing live in a meeting, either open the link a minute early, or
  upgrade the Render service to Starter (~€7/month) for that one meeting
  and downgrade after — no long-term commitment needed.

## Alternatives, if this combination doesn't fit

- **Railway** — similarly easy, autodetects the app, but its free
  allowance is a one-time trial credit rather than an ongoing free tier;
  fine for a short demo window, becomes a paid product after the credit
  runs out.
- **Fly.io** — no longer offers a free tier for new accounts as of 2026;
  skip unless you specifically want its global-edge deployment model.
- **A single Hetzner/DigitalOcean VPS running everything** (Postgres +
  app in Docker) — cheaper long-term (~€4-6/month) if this moves past the
  proposal stage into something Waterpower actually keeps running, but
  more setup work than a proposal demo justifies right now.

Pricing/limits above were checked in August 2026 — hosting free tiers
change often, so it's worth a quick check of Render's and Neon's own
pricing pages before you commit if it's been a while since this was
written.
