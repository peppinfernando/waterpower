"""Cost calculation and rollups.

Cost for an interval = settlement price (EUR/MWh) * volume (MWh).

Until real usage data is connected, volume falls back to a notional flat
load (NOTIONAL_LOAD_MW, default 1 MW) so the cost views show the *shape* of
wholesale exposure — i.e. the price curve itself — which is exactly what's
needed to start reasoning about time-of-use tariffs, even before actual
consumption is known. The moment a row exists in `usage_intervals` for a
given interval, it takes over automatically.
"""
import os
from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SettlementPrice, UsageInterval, CostRecord

NOTIONAL_LOAD_MW = float(os.getenv("NOTIONAL_LOAD_MW", "1.0"))


def rebuild_cost_records(db: Session, start: datetime, end: datetime) -> int:
    """Upserts CostRecord rows for [start, end) from prices + (actual or
    notional) usage. Returns the number of rows written."""
    prices = db.scalars(
        select(SettlementPrice).where(
            SettlementPrice.interval_start >= start,
            SettlementPrice.interval_start < end,
        )
    ).all()

    usage_by_start = {
        u.interval_start: u.volume_mwh
        for u in db.scalars(
            select(UsageInterval).where(
                UsageInterval.interval_start >= start,
                UsageInterval.interval_start < end,
            )
        ).all()
    }

    written = 0
    for p in prices:
        actual_volume = usage_by_start.get(p.interval_start)
        is_actual = actual_volume is not None
        volume = actual_volume if is_actual else NOTIONAL_LOAD_MW * (p.interval_minutes / 60)
        cost = round(p.price_eur_per_mwh * volume, 4)

        existing = db.scalar(
            select(CostRecord).where(
                CostRecord.interval_start == p.interval_start,
                CostRecord.interval_minutes == p.interval_minutes,
            )
        )
        if existing:
            existing.price_eur_per_mwh = p.price_eur_per_mwh
            existing.volume_mwh = volume
            existing.volume_is_actual = "actual" if is_actual else "notional"
            existing.cost_eur = cost
        else:
            db.add(CostRecord(
                interval_start=p.interval_start,
                interval_minutes=p.interval_minutes,
                price_eur_per_mwh=p.price_eur_per_mwh,
                volume_mwh=volume,
                volume_is_actual="actual" if is_actual else "notional",
                cost_eur=cost,
            ))
        written += 1

    db.commit()
    return written


def get_daily_costs(db: Session, day: date) -> List[CostRecord]:
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    rebuild_cost_records(db, start, end)
    return db.scalars(
        select(CostRecord)
        .where(CostRecord.interval_start >= start, CostRecord.interval_start < end)
        .order_by(CostRecord.interval_start)
    ).all()


def get_daily_costs_hourly(db: Session, day: date) -> List[dict]:
    """Merges the two 30-min settlement periods within each clock hour into
    one row: volume-weighted average price, summed volume, summed cost.
    24 rows per day instead of 48."""
    records = get_daily_costs(db, day)

    by_hour: dict[int, dict] = {}
    for r in records:
        h = r.interval_start.hour
        bucket = by_hour.setdefault(h, {"cost_eur": 0.0, "volume_mwh": 0.0})
        bucket["cost_eur"] += r.cost_eur
        bucket["volume_mwh"] += r.volume_mwh

    result = []
    for h in sorted(by_hour):
        b = by_hour[h]
        avg_price = b["cost_eur"] / b["volume_mwh"] if b["volume_mwh"] else 0
        result.append({
            "hour": h,
            "hour_label": f"{h:02d}:00",
            "price_eur_per_mwh": round(avg_price, 2),
            "volume_mwh": round(b["volume_mwh"], 3),
            "cost_eur": round(b["cost_eur"], 2),
        })
    return result


def get_price_band_summary(hourly: List[dict], band_width: float = 10.0) -> List[dict]:
    """Groups hours into price bands (e.g. €60-70/MWh) and counts how many
    hours fell in each band, alongside the band's total cost. Lets you see
    at a glance e.g. "6 hours were in the €80-90 band" rather than reading
    24 individual hourly prices."""
    bands: dict[float, dict] = {}
    for h in hourly:
        band_floor = (h["price_eur_per_mwh"] // band_width) * band_width
        bucket = bands.setdefault(band_floor, {"hours": 0, "cost_eur": 0.0})
        bucket["hours"] += 1
        bucket["cost_eur"] += h["cost_eur"]

    result = []
    for band_floor in sorted(bands):
        b = bands[band_floor]
        result.append({
            "band": f"€{band_floor:.0f}-{band_floor + band_width:.0f}",
            "hours": b["hours"],
            "cost_eur": round(b["cost_eur"], 2),
        })
    return result


def get_range_costs_by_day(db: Session, start_day: date, end_day: date) -> List[dict]:
    """Aggregates cost/volume/avg-price per calendar day across a date range
    (used by both the weekly and monthly views)."""
    start = datetime.combine(start_day, datetime.min.time())
    end = datetime.combine(end_day, datetime.min.time())
    rebuild_cost_records(db, start, end)

    records = db.scalars(
        select(CostRecord)
        .where(CostRecord.interval_start >= start, CostRecord.interval_start < end)
        .order_by(CostRecord.interval_start)
    ).all()

    by_day: dict[date, dict] = {}
    for r in records:
        d = r.interval_start.date()
        bucket = by_day.setdefault(d, {"cost_eur": 0.0, "volume_mwh": 0.0})
        bucket["cost_eur"] += r.cost_eur
        bucket["volume_mwh"] += r.volume_mwh

    result = []
    for d in sorted(by_day):
        c = by_day[d]
        avg_price = c["cost_eur"] / c["volume_mwh"] if c["volume_mwh"] else 0
        result.append({
            "date": d,
            "cost_eur": round(c["cost_eur"], 2),
            "volume_mwh": round(c["volume_mwh"], 3),
            "average_price_eur_per_mwh": round(avg_price, 2),
        })
    return result


def get_yearly_costs_by_month(db: Session, year: int) -> List[dict]:
    """Aggregates cost/volume/avg-price per calendar month across a year —
    the rollup used by the Yearly view. Drilling into a given month reuses
    get_range_costs_by_day (the same aggregation the Monthly view uses)."""
    start_day = date(year, 1, 1)
    end_day = date(year + 1, 1, 1)
    days = get_range_costs_by_day(db, start_day, end_day)

    by_month: dict[int, dict] = {}
    for d in days:
        m = d["date"].month
        bucket = by_month.setdefault(m, {"cost_eur": 0.0, "volume_mwh": 0.0})
        bucket["cost_eur"] += d["cost_eur"]
        bucket["volume_mwh"] += d["volume_mwh"]

    result = []
    for m in sorted(by_month):
        b = by_month[m]
        avg_price = b["cost_eur"] / b["volume_mwh"] if b["volume_mwh"] else 0
        result.append({
            "month": m,
            "month_label": date(year, m, 1).strftime("%b"),
            "cost_eur": round(b["cost_eur"], 2),
            "volume_mwh": round(b["volume_mwh"], 3),
            "average_price_eur_per_mwh": round(avg_price, 2),
        })
    return result


def get_range_costs_auto(db: Session, start_day: date, end_day: date) -> dict:
    """Generic custom-range rollup for the free From/To picker. Picks a
    granularity automatically based on the span so a 3-day range isn't
    crammed into 3 bars and a 400-day range isn't 400 daily bars:
      - up to 1 day  -> hourly (reuses the daily hourly rollup)
      - up to ~13 weeks (92 days) -> daily rollup
      - longer -> monthly rollup
    """
    span_days = (end_day - start_day).days
    if span_days <= 1:
        hourly = get_daily_costs_hourly(db, start_day)
        return {
            "granularity": "hourly",
            "points": [{"label": h["hour_label"], "cost_eur": h["cost_eur"],
                        "volume_mwh": h["volume_mwh"], "average_price_eur_per_mwh": h["price_eur_per_mwh"]}
                       for h in hourly],
        }
    if span_days <= 92:
        days = get_range_costs_by_day(db, start_day, end_day)
        return {
            "granularity": "daily",
            "points": [{"label": d["date"].isoformat(), "cost_eur": d["cost_eur"],
                        "volume_mwh": d["volume_mwh"], "average_price_eur_per_mwh": d["average_price_eur_per_mwh"]}
                       for d in days],
        }

    days = get_range_costs_by_day(db, start_day, end_day)
    by_month: dict[str, dict] = {}
    for d in days:
        key = d["date"].strftime("%Y-%m")
        bucket = by_month.setdefault(key, {"cost_eur": 0.0, "volume_mwh": 0.0})
        bucket["cost_eur"] += d["cost_eur"]
        bucket["volume_mwh"] += d["volume_mwh"]
    points = []
    for key in sorted(by_month):
        b = by_month[key]
        avg_price = b["cost_eur"] / b["volume_mwh"] if b["volume_mwh"] else 0
        points.append({"label": key, "cost_eur": round(b["cost_eur"], 2),
                        "volume_mwh": round(b["volume_mwh"], 3),
                        "average_price_eur_per_mwh": round(avg_price, 2)})
    return {"granularity": "monthly", "points": points}


def _period_bounds(period: str, anchor: date, year_offset: int, custom_end: Optional[date] = None) -> tuple[date, date, str]:
    """Computes the [start, end) window and a display label for a given
    period type, shifted back `year_offset` years from `anchor`, aligned by
    RELATIVE period (same ISO week number / same calendar month) rather
    than exact calendar date — see the design note in routers/costs.py.

    "custom" shifts an arbitrary [anchor, custom_end) window back by whole
    years using calendar-date arithmetic, since an arbitrary range has no
    natural "relative period" (no week/month number to align on) the way
    day/week/month do. Both boundaries fall back a day for Feb 29 in a
    non-leap target year, same as the "day" case.
    """
    target_year = anchor.year - year_offset

    if period == "day":
        # Same month/day each year; Feb 29 falls back to Feb 28 on non-leap years.
        try:
            d = anchor.replace(year=target_year)
        except ValueError:
            d = date(target_year, 2, 28)
        return d, d + timedelta(days=1), d.isoformat()

    if period == "week":
        iso_year, iso_week, _ = anchor.isocalendar()
        try:
            start = date.fromisocalendar(target_year, iso_week, 1)
        except ValueError:
            # some years don't have a week 53; fall back to week 52
            start = date.fromisocalendar(target_year, min(iso_week, 52), 1)
        return start, start + timedelta(days=7), f"{target_year} wk{iso_week}"

    if period == "month":
        start = date(target_year, anchor.month, 1)
        end = date(target_year + 1, 1, 1) if anchor.month == 12 else date(target_year, anchor.month + 1, 1)
        return start, end, start.strftime("%b %Y")

    if period == "year":
        return date(target_year, 1, 1), date(target_year + 1, 1, 1), str(target_year)

    if period == "custom":
        def shift(d: date) -> date:
            try:
                return d.replace(year=d.year - year_offset)
            except ValueError:
                return date(d.year - year_offset, 2, 28)  # Feb 29 fallback
        start, end = shift(anchor), shift(custom_end)
        return start, end, f"{start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"

    raise ValueError(f"unsupported period: {period}")


def compare_periods(db: Session, period: str, anchor: date, years_back: int = 5, custom_end: Optional[date] = None) -> dict:
    """Builds year-over-year comparison series for a day/week/month/year/
    custom period, anchored on `anchor` (the currently-selected period,
    or its start for "custom") plus each of the `years_back` prior years
    at the same RELATIVE period. Years with no ingested price data are
    silently omitted rather than failing the whole request, since 5 years
    of history may not always be backfilled yet.
    """
    series = []
    baseline_total = None

    for offset in range(0, years_back + 1):
        start, end, label = _period_bounds(period, anchor, offset, custom_end)

        has_data = db.scalar(
            select(SettlementPrice.id).where(
                SettlementPrice.interval_start >= datetime.combine(start, datetime.min.time()),
                SettlementPrice.interval_start < datetime.combine(end, datetime.min.time()),
            ).limit(1)
        )
        if not has_data:
            continue

        if period == "day":
            points_raw = get_daily_costs_hourly(db, start)
            points = [{"x": p["hour_label"], "cost_eur": p["cost_eur"]} for p in points_raw]
        elif period == "year":
            months = get_yearly_costs_by_month(db, start.year)
            points = [{"x": m["month_label"], "cost_eur": m["cost_eur"]} for m in months]
        else:  # week, month, custom — all day-by-day
            days = get_range_costs_by_day(db, start, end)
            if period == "week":
                x_labels = [d["date"].strftime("%a") for d in days]
            elif period == "month":
                x_labels = [str(d["date"].day) for d in days]
            else:  # custom — index into the range, since dates themselves differ year to year
                x_labels = [str(i + 1) for i in range(len(days))]
            points = [{"x": x, "cost_eur": d["cost_eur"]} for x, d in zip(x_labels, days)]

        total_cost = sum(p["cost_eur"] for p in points)
        total_volume = (
            sum(h["volume_mwh"] for h in get_daily_costs_hourly(db, start)) if period == "day"
            else sum(d["volume_mwh"] for d in get_range_costs_by_day(db, start, end))
        )
        avg_price = total_cost / total_volume if total_volume else 0

        if offset == 0:
            baseline_total = total_cost

        variance_pct = (
            round((baseline_total - total_cost) / total_cost * 100, 1)
            if (offset > 0 and baseline_total is not None and total_cost)
            else None
        )

        series.append({
            "year": anchor.year - offset,
            "label": label,
            "is_current": offset == 0,
            "total_cost_eur": round(total_cost, 2),
            "average_price_eur_per_mwh": round(avg_price, 2),
            "variance_pct_vs_current": variance_pct,
            "points": points,
        })

    return {"period": period, "anchor": anchor.isoformat(), "series": series}
