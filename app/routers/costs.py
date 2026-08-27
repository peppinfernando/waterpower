from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import aggregation
from app import schemas

router = APIRouter(prefix="/api/costs", tags=["costs"])


def _set_cache_headers(response: Response, range_end_exclusive: date):
    """Historical data (any range ending before today) can't change once
    computed, so it's safe to let the browser cache it for an hour and
    skip the network entirely on repeat views — the auto-refresh timer,
    revisiting the same date, switching tabs and back, etc. Anything
    touching today or the future stays uncached since new settlement
    prices are still arriving for those periods."""
    if range_end_exclusive <= date.today():
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"


@router.get("/daily", response_model=schemas.DailyCostResponse)
def daily(date_: date, response: Response, db: Session = Depends(get_db)):
    records = aggregation.get_daily_costs(db, date_)
    if not records:
        raise HTTPException(404, f"No settlement price data for {date_}")

    total_cost = sum(r.cost_eur for r in records)
    total_volume = sum(r.volume_mwh for r in records)
    prices = [r.price_eur_per_mwh for r in records]

    hourly = aggregation.get_daily_costs_hourly(db, date_)
    price_bands = aggregation.get_price_band_summary(hourly)

    _set_cache_headers(response, date_ + timedelta(days=1))

    return schemas.DailyCostResponse(
        date=date_,
        intervals=[
            schemas.IntervalCost(
                interval_start=r.interval_start,
                price_eur_per_mwh=r.price_eur_per_mwh,
                volume_mwh=r.volume_mwh,
                volume_is_actual=(r.volume_is_actual == "actual"),
                cost_eur=r.cost_eur,
            )
            for r in records
        ],
        hourly=[schemas.HourCost(**h) for h in hourly],
        price_bands=[schemas.PriceBand(**b) for b in price_bands],
        total_cost_eur=round(total_cost, 2),
        total_volume_mwh=round(total_volume, 3),
        average_price_eur_per_mwh=round(total_cost / total_volume, 2) if total_volume else 0,
        peak_interval_price_eur_per_mwh=max(prices),
        off_peak_interval_price_eur_per_mwh=min(prices),
    )


@router.get("/weekly", response_model=schemas.WeeklyCostResponse)
def weekly(week_start: date, response: Response, db: Session = Depends(get_db)):
    week_end = week_start + timedelta(days=7)
    days = aggregation.get_range_costs_by_day(db, week_start, week_end)
    if not days:
        raise HTTPException(404, f"No data for week starting {week_start}")

    total_cost = sum(d["cost_eur"] for d in days)
    total_volume = sum(d["volume_mwh"] for d in days)

    _set_cache_headers(response, week_end)

    return schemas.WeeklyCostResponse(
        week_start=week_start,
        week_end=week_end - timedelta(days=1),
        days=[schemas.DayCost(**d) for d in days],
        total_cost_eur=round(total_cost, 2),
        average_price_eur_per_mwh=round(total_cost / total_volume, 2) if total_volume else 0,
    )


@router.get("/yearly", response_model=schemas.YearlyCostResponse)
def yearly(year: int, response: Response, db: Session = Depends(get_db)):
    months = aggregation.get_yearly_costs_by_month(db, year)
    if not months:
        raise HTTPException(404, f"No data for {year}")

    total_cost = sum(m["cost_eur"] for m in months)
    total_volume = sum(m["volume_mwh"] for m in months)

    _set_cache_headers(response, date(year + 1, 1, 1))

    return schemas.YearlyCostResponse(
        year=year,
        months=[schemas.MonthCost(**m) for m in months],
        total_cost_eur=round(total_cost, 2),
        average_price_eur_per_mwh=round(total_cost / total_volume, 2) if total_volume else 0,
    )


@router.get("/range", response_model=schemas.RangeCostResponse)
def range_(start: date, end: date, response: Response, db: Session = Depends(get_db)):
    """Free-form From/To range for the custom date picker. Granularity is
    picked automatically based on the span (see get_range_costs_auto):
    a 1-day range returns hourly points, a multi-week/month range returns
    daily points, and anything longer than ~13 weeks rolls up to monthly —
    so a year-long custom range doesn't render as 365 illegible bars."""
    if end <= start:
        raise HTTPException(400, "end must be after start")

    result = aggregation.get_range_costs_auto(db, start, end)
    if not result["points"]:
        raise HTTPException(404, f"No data for {start} to {end}")

    total_cost = sum(p["cost_eur"] for p in result["points"])
    total_volume = sum(p["volume_mwh"] for p in result["points"])

    _set_cache_headers(response, end)

    return schemas.RangeCostResponse(
        start=start,
        end=end,
        granularity=result["granularity"],
        points=[schemas.RangePoint(**p) for p in result["points"]],
        total_cost_eur=round(total_cost, 2),
        average_price_eur_per_mwh=round(total_cost / total_volume, 2) if total_volume else 0,
    )


@router.get("/compare", response_model=schemas.ComparisonResponse)
def compare(
    period: str,
    anchor: date,
    years: int = 5,
    end: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """Year-over-year comparison for forecasting/trend analysis. `anchor`
    is the currently-selected day/week/month/year (or the start of a
    custom range, with `end` required for period="custom"). Prior years
    are matched by RELATIVE period (same ISO week number for weekly, same
    calendar month for monthly, same month/day for daily, same calendar
    year for yearly) rather than exact calendar date, so weekday alignment
    holds across years. A custom range is shifted back by whole calendar
    years instead, since an arbitrary span has no natural week/month
    number to align on. Years with no ingested price data are silently
    skipped rather than erroring the whole request."""
    if period not in ("day", "week", "month", "year", "custom"):
        raise HTTPException(400, "period must be one of: day, week, month, year, custom")
    if period == "custom" and end is None:
        raise HTTPException(400, "end is required when period=custom")

    result = aggregation.compare_periods(db, period, anchor, years, custom_end=end)
    if not result["series"]:
        raise HTTPException(404, "No data available for this period in any year")

    return schemas.ComparisonResponse(**result)


@router.get("/monthly", response_model=schemas.MonthlyCostResponse)
def monthly(year: int, month: int, response: Response, db: Session = Depends(get_db)):
    start_day = date(year, month, 1)
    end_day = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days = aggregation.get_range_costs_by_day(db, start_day, end_day)
    if not days:
        raise HTTPException(404, f"No data for {year}-{month:02d}")

    total_cost = sum(d["cost_eur"] for d in days)
    total_volume = sum(d["volume_mwh"] for d in days)

    _set_cache_headers(response, end_day)

    return schemas.MonthlyCostResponse(
        year=year,
        month=month,
        days=[schemas.DayCost(**d) for d in days],
        total_cost_eur=round(total_cost, 2),
        average_price_eur_per_mwh=round(total_cost / total_volume, 2) if total_volume else 0,
    )
