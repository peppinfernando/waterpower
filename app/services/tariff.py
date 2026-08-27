"""Turns historical wholesale cost data into a proposed time-of-use retail
tariff with a target margin applied on top of actual procurement cost.

This is deliberately simple for v1: three fixed bands (night/day/peak).
Swap in a more sophisticated banding (e.g. clustering price data into
bands automatically) once there's enough history to justify it.
"""
from datetime import date, datetime, timedelta
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CostRecord
from app.services.aggregation import rebuild_cost_records

# hour ranges are inclusive-start, exclusive-end, in local wall-clock hours
DEFAULT_BANDS = [
    {"name": "night", "hours": "23:00-08:00", "hour_ranges": [(23, 24), (0, 8)]},
    {"name": "day", "hours": "08:00-17:00", "hour_ranges": [(8, 17)]},
    {"name": "peak", "hours": "17:00-23:00", "hour_ranges": [(17, 23)]},
]

EUR_MWH_TO_EUR_KWH = 0.001


def _in_band(hour: int, band: dict) -> bool:
    return any(start <= hour < end for start, end in band["hour_ranges"])


def calculate_tariff(
    db: Session,
    start_day: date,
    end_day: date,
    target_margin_pct: float = 20.0,
    bands: List[dict] = None,
) -> dict:
    bands = bands or DEFAULT_BANDS
    start = datetime.combine(start_day, datetime.min.time())
    end = datetime.combine(end_day, datetime.min.time())
    rebuild_cost_records(db, start, end)

    records = db.scalars(
        select(CostRecord)
        .where(CostRecord.interval_start >= start, CostRecord.interval_start < end)
    ).all()

    band_totals = {b["name"]: {"cost": 0.0, "volume": 0.0} for b in bands}
    total_cost, total_volume = 0.0, 0.0

    for r in records:
        total_cost += r.cost_eur
        total_volume += r.volume_mwh
        for b in bands:
            if _in_band(r.interval_start.hour, b):
                band_totals[b["name"]]["cost"] += r.cost_eur
                band_totals[b["name"]]["volume"] += r.volume_mwh
                break

    periods = []
    for b in bands:
        bt = band_totals[b["name"]]
        avg_wholesale = bt["cost"] / bt["volume"] if bt["volume"] else 0.0
        retail_eur_per_mwh = avg_wholesale * (1 + target_margin_pct / 100)
        periods.append({
            "name": b["name"],
            "hours": b["hours"],
            "avg_wholesale_price_eur_per_mwh": round(avg_wholesale, 2),
            "proposed_retail_price_eur_per_kwh": round(retail_eur_per_mwh * EUR_MWH_TO_EUR_KWH, 4),
            "margin_pct": target_margin_pct,
        })

    blended = total_cost / total_volume if total_volume else 0.0

    return {
        "based_on": f"{start_day.isoformat()} to {end_day.isoformat()}",
        "periods": periods,
        "blended_wholesale_price_eur_per_mwh": round(blended, 2),
    }
