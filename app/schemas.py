from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class IntervalCost(BaseModel):
    interval_start: datetime
    price_eur_per_mwh: float
    volume_mwh: float
    volume_is_actual: bool
    cost_eur: float


class HourCost(BaseModel):
    hour: int
    hour_label: str
    price_eur_per_mwh: float
    volume_mwh: float
    cost_eur: float


class PriceBand(BaseModel):
    band: str
    hours: int
    cost_eur: float


class DailyCostResponse(BaseModel):
    date: date
    intervals: List[IntervalCost]  # raw 30-min settlement periods
    hourly: List[HourCost]  # same day, merged into 24 hourly rows
    price_bands: List[PriceBand]  # hour counts grouped by price band
    total_cost_eur: float
    total_volume_mwh: float
    average_price_eur_per_mwh: float
    peak_interval_price_eur_per_mwh: Optional[float] = None
    off_peak_interval_price_eur_per_mwh: Optional[float] = None


class DayCost(BaseModel):
    date: date
    cost_eur: float
    volume_mwh: float
    average_price_eur_per_mwh: float


class WeeklyCostResponse(BaseModel):
    week_start: date
    week_end: date
    days: List[DayCost]
    total_cost_eur: float
    average_price_eur_per_mwh: float


class MonthlyCostResponse(BaseModel):
    year: int
    month: int
    days: List[DayCost]
    total_cost_eur: float
    average_price_eur_per_mwh: float


class MonthCost(BaseModel):
    month: int
    month_label: str
    cost_eur: float
    volume_mwh: float
    average_price_eur_per_mwh: float


class YearlyCostResponse(BaseModel):
    year: int
    months: List[MonthCost]
    total_cost_eur: float
    average_price_eur_per_mwh: float


class RangePoint(BaseModel):
    label: str
    cost_eur: float
    volume_mwh: float
    average_price_eur_per_mwh: float


class RangeCostResponse(BaseModel):
    start: date
    end: date
    granularity: str  # "hourly" | "daily" | "monthly" — auto-picked by span
    points: List[RangePoint]
    total_cost_eur: float
    average_price_eur_per_mwh: float


class ComparisonPoint(BaseModel):
    x: str
    cost_eur: float


class ComparisonSeries(BaseModel):
    year: int
    label: str
    is_current: bool
    total_cost_eur: float
    average_price_eur_per_mwh: float
    variance_pct_vs_current: Optional[float] = None  # null for the current (baseline) series
    points: List[ComparisonPoint]


class ComparisonResponse(BaseModel):
    period: str  # "day" | "week" | "month"
    anchor: date
    series: List[ComparisonSeries]


class TariffPeriod(BaseModel):
    name: str  # e.g. "peak", "day", "night"
    hours: str  # human readable, e.g. "17:00-19:00"
    avg_wholesale_price_eur_per_mwh: float
    proposed_retail_price_eur_per_kwh: float
    margin_pct: float


class TariffResponse(BaseModel):
    based_on: str  # e.g. "2026-08 monthly data"
    periods: List[TariffPeriod]
    blended_wholesale_price_eur_per_mwh: float
