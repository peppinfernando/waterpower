"""Backfills mock SEM-O settlement prices so the dashboard has data to show
before real ingestion or SEM-O portal access is set up.

Usage:
    python seed_mock_data.py                # last 30 days (default)
    python seed_mock_data.py --days 1825     # last 5 years, for year-over-year comparison

Uses a delete-then-bulk-insert per date range rather than per-row existence
checks, since 5 years is ~87,600 rows and a per-row SELECT would make that
painfully slow. Safe to re-run — it clears and regenerates the given range.
"""
import argparse
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.database import Base, engine, session_scope
from app.ingestion.sem_o_client import MockSemOClient
from app.models import SettlementPrice

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=30, help="How many days of history to backfill (default 30)")
args = parser.parse_args()

Base.metadata.create_all(bind=engine)

client = MockSemOClient()
end = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
start = end - timedelta(days=args.days)

print(f"Generating {args.days} days of mock settlement prices ({start.date()} to {end.date()})...")

# Generate + insert one day at a time so memory stays flat even for 5 years.
total_written = 0
day_cursor = start
while day_cursor < end:
    day_end = min(day_cursor + timedelta(days=1), end)
    points = client.fetch_settlement_prices(day_cursor, day_end)

    with session_scope() as db:
        db.execute(delete(SettlementPrice).where(
            SettlementPrice.interval_start >= day_cursor,
            SettlementPrice.interval_start < day_end,
        ))
        db.bulk_save_objects([
            SettlementPrice(
                interval_start=p.interval_start,
                interval_minutes=p.interval_minutes,
                price_eur_per_mwh=p.price_eur_per_mwh,
                net_imbalance_volume_mwh=p.net_imbalance_volume_mwh,
            )
            for p in points
        ])
    total_written += len(points)
    day_cursor += timedelta(days=1)

print(f"Seeded {total_written} settlement price intervals.")
if args.days >= 365:
    print("5-year backfill complete — the historical comparison feature (day/week/month "
          "vs. same period in prior years) now has data to compare against.")
