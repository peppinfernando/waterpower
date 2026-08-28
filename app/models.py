"""ORM models.

Design notes:
- `SettlementPrice` stores one row per 30-minute Imbalance Settlement Period
  (ISP), which is how SEM-O publishes BM prices. If SEM-O later moves to
  15-minute periods (as some other European markets have), only the
  `interval_minutes` field and ingestion parsing need to change.
- `UsageInterval` is intentionally minimal and unpopulated for v1 — it's
  here so cost calculation has a real table to join against the moment
  usage data becomes available, without a schema migration on that day.
- `CostRecord` is a derived/materialized table (price * volume per interval)
  rather than something computed on every request, so the daily/weekly/
  monthly views stay fast as history grows. It's rebuilt by the aggregation
  service whenever new prices or usage land.
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, Float, DateTime, String, UniqueConstraint, Index
)

from app.database import Base


class SettlementPrice(Base):
    """One BM Imbalance Settlement Price per interval, as published by SEM-O."""
    __tablename__ = "settlement_prices"

    id = Column(Integer, primary_key=True)
    interval_start = Column(DateTime, nullable=False, index=True)  # UTC
    interval_minutes = Column(Integer, nullable=False, default=30)
    price_eur_per_mwh = Column(Float, nullable=False)
    net_imbalance_volume_mwh = Column(Float, nullable=True)
    source = Column(String, nullable=False, default="sem-o")
    ingested_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("interval_start", "interval_minutes", name="uq_price_interval"),
    )


class UsageInterval(Base):
    """Supplier-aggregate metered consumption per interval. Empty until the
    usage feed is connected; cost calc falls back to a notional load until
    then (see services/aggregation.py)."""
    __tablename__ = "usage_intervals"

    id = Column(Integer, primary_key=True)
    interval_start = Column(DateTime, nullable=False, index=True)
    interval_minutes = Column(Integer, nullable=False, default=30)
    volume_mwh = Column(Float, nullable=False)
    source = Column(String, nullable=False, default="unknown")

    __table_args__ = (
        UniqueConstraint("interval_start", "interval_minutes", name="uq_usage_interval"),
    )


class CostRecord(Base):
    """Derived: procurement cost for one interval = price * volume.
    Rebuilt/upserted by the aggregation service, read by the API."""
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True)
    interval_start = Column(DateTime, nullable=False, index=True)
    interval_minutes = Column(Integer, nullable=False, default=30)
    price_eur_per_mwh = Column(Float, nullable=False)
    volume_mwh = Column(Float, nullable=False)
    volume_is_actual = Column(String, nullable=False, default="notional")  # "actual" | "notional"
    cost_eur = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("interval_start", "interval_minutes", name="uq_cost_interval"),
        Index("ix_cost_interval_start", "interval_start"),
    )


class ActiveSession(Base):
    """Server-side session record — what actually makes a session
    "killable". A signed cookie alone (the previous auth design) can't be
    revoked once issued; the browser holds a valid, unforgeable token and
    there's no server-side state to invalidate short of rotating
    SESSION_SECRET, which kills every session for every account at once.

    Each login creates exactly one row here, and the cookie carries only
    an opaque session_id pointing at it — so a login can be killed
    individually (this account only), collectively (kill-all), or expire
    on its own (idle timeout / absolute lifetime), all by deleting or
    aging out this row, with the cookie itself never needing to change."""
    __tablename__ = "active_sessions"

    session_id = Column(String, primary_key=True)  # secrets.token_urlsafe(32)
    username = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
