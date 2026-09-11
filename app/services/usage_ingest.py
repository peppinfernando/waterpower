"""Parses and ingests utility usage data (the file Waterpower is expected
to supply): actual metered electricity consumption per 30-minute interval,
which the cost engine then uses in place of the notional flat-load
estimate for any interval it covers (see aggregation.py).

No real sample file exists yet, so the parser is deliberately forgiving
of common column-naming and date-format variations rather than locked to
one exact schema — the goal is "probably works with little or no
adjustment" once a real file shows up, not "must match exactly."

Expected shape (columns detected case-insensitively, order doesn't
matter):
  - A timestamp, either as one combined column (interval_start /
    timestamp / datetime / date_time) or as separate date + time columns.
  - A usage column (usage_kwh / kwh / consumption_kwh / usage /
    consumption / volume_kwh / reading_kwh / energy_kwh), values in kWh
    unless the column name itself says "mwh". 30-minute intervals are
    the expectation (matching the settlement price data's own
    granularity) but not strictly enforced — the interval length is
    inferred from the gaps between timestamps, and a file doesn't need
    to cover exactly 48 rows/day; partial days and gaps are fine.
"""
import io
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CostRecord, UsageInterval

TIMESTAMP_ALIASES = ["interval_start", "timestamp", "datetime", "date_time", "reading_time", "time_stamp"]
DATE_ALIASES = ["date", "reading_date"]
TIME_ALIASES = ["time", "start_time", "interval_time"]
USAGE_ALIASES = [
    "usage_kwh", "kwh", "consumption_kwh", "usage", "consumption",
    "volume_kwh", "reading_kwh", "kwh_usage", "energy_kwh",
    "volume_mwh", "usage_mwh", "mwh",
]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB — generous for a month of half-hourly readings, cheap to sanity-cap


def _normalize(col: str) -> str:
    return str(col).strip().lower().replace(" ", "_").replace("-", "_")


def _find_column(cols_by_normalized: Dict[str, str], aliases: List[str]) -> Optional[str]:
    for alias in aliases:
        if alias in cols_by_normalized:
            return cols_by_normalized[alias]
    return None


def _infer_interval_minutes(sorted_starts: List[datetime]) -> int:
    """Looks at gaps between consecutive timestamps and returns the most
    common one, in minutes. Defaults to 30 (matching the settlement price
    data) if there's nothing usable to infer from — e.g. a single-row
    file, or if every gap looks like a day boundary rather than a
    within-day reading interval."""
    if len(sorted_starts) < 2:
        return 30
    gaps = []
    for i in range(1, min(len(sorted_starts), 200)):  # sample is plenty; no need to scan a huge file
        delta_minutes = (sorted_starts[i] - sorted_starts[i - 1]).total_seconds() / 60
        if 0 < delta_minutes <= 120:  # sane within-day gap; ignores day-to-day jumps
            gaps.append(round(delta_minutes))
    if not gaps:
        return 30
    return Counter(gaps).most_common(1)[0][0]


def parse_usage_csv(file_bytes: bytes) -> Tuple[List[dict], List[str], List[str]]:
    """Returns (records, warnings, errors). `records` is [] whenever
    `errors` is non-empty — callers should check errors first and stop
    there rather than trying to use a partial/empty records list."""
    warnings: List[str] = []

    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as e:
        return [], [], [f"Could not read this as a CSV file ({e})."]

    if df.empty or len(df.columns) == 0:
        return [], [], ["The file has no data rows."]

    cols_by_normalized = {_normalize(c): c for c in df.columns}

    ts_col = _find_column(cols_by_normalized, TIMESTAMP_ALIASES)
    date_col = _find_column(cols_by_normalized, DATE_ALIASES)
    time_col = _find_column(cols_by_normalized, TIME_ALIASES)
    usage_col = _find_column(cols_by_normalized, USAGE_ALIASES)

    if not usage_col:
        return [], [], [
            "Couldn't find a usage column. Expected something like 'usage_kwh' or 'kWh'. "
            f"Columns found: {', '.join(str(c) for c in df.columns)}"
        ]
    if not ts_col and not (date_col and time_col):
        return [], [], [
            "Couldn't find a timestamp column (or separate date + time columns). "
            f"Columns found: {', '.join(str(c) for c in df.columns)}"
        ]

    usage_col_normalized = next(k for k, v in cols_by_normalized.items() if v == usage_col)
    is_mwh = "mwh" in usage_col_normalized

    # Parsing dates from an unknown source is inherently ambiguous between
    # DD/MM and MM/DD — try the default (month-first) parse, and if that
    # leaves a lot of unparseable rows, retry day-first and keep
    # whichever interpretation parsed more successfully.
    def _parse_datetimes(raw_series) -> "pd.Series":
        attempt1 = pd.to_datetime(raw_series, errors="coerce")
        if attempt1.isna().mean() <= 0.05:
            return attempt1
        attempt2 = pd.to_datetime(raw_series, errors="coerce", dayfirst=True)
        return attempt2 if attempt2.isna().sum() < attempt1.isna().sum() else attempt1

    if ts_col:
        ts_series = _parse_datetimes(df[ts_col])
    else:
        combined = df[date_col].astype(str) + " " + df[time_col].astype(str)
        ts_series = _parse_datetimes(combined)

    usage_series = pd.to_numeric(df[usage_col], errors="coerce")

    records = []
    skipped = 0
    for i in range(len(df)):
        ts = ts_series.iloc[i]
        val = usage_series.iloc[i]
        if pd.isna(ts) or pd.isna(val):
            skipped += 1
            continue
        volume_mwh = float(val) if is_mwh else float(val) / 1000.0
        if volume_mwh < 0:
            skipped += 1
            continue
        records.append({"interval_start": ts.to_pydatetime().replace(tzinfo=None), "volume_mwh": volume_mwh})

    if skipped:
        warnings.append(f"Skipped {skipped} row(s) with an unreadable timestamp, missing usage value, or negative usage.")

    if not records:
        return [], warnings, ["No valid rows could be parsed — check the timestamp and usage columns have real values."]

    records.sort(key=lambda r: r["interval_start"])
    interval_minutes = _infer_interval_minutes([r["interval_start"] for r in records])
    for r in records:
        r["interval_minutes"] = interval_minutes

    seen = set()
    deduped = []
    dup_count = 0
    for r in records:
        key = (r["interval_start"], r["interval_minutes"])
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        deduped.append(r)
    if dup_count:
        warnings.append(f"{dup_count} duplicate timestamp(s) found — kept the last occurrence of each.")

    return deduped, warnings, []


def upsert_usage_intervals(db: Session, records: List[dict], source: str = "upload") -> dict:
    """Bulk upsert, not one-query-per-row — same pattern as
    rebuild_cost_records's own fix earlier in this project. Fetches
    every existing row in the affected range in a single query, then
    diffs in Python, rather than a SELECT per record."""
    if not records:
        return {"inserted": 0, "updated": 0, "date_range_start": None, "date_range_end": None}

    starts = [r["interval_start"] for r in records]
    min_start, max_start = min(starts), max(starts)

    existing = {
        (u.interval_start, u.interval_minutes): u
        for u in db.scalars(
            select(UsageInterval).where(
                UsageInterval.interval_start >= min_start,
                UsageInterval.interval_start <= max_start,
            )
        ).all()
    }

    inserted, updated = 0, 0
    new_rows = []
    for r in records:
        key = (r["interval_start"], r["interval_minutes"])
        existing_row = existing.get(key)
        if existing_row:
            existing_row.volume_mwh = r["volume_mwh"]
            existing_row.source = source
            updated += 1
        else:
            new_rows.append(UsageInterval(
                interval_start=r["interval_start"],
                interval_minutes=r["interval_minutes"],
                volume_mwh=r["volume_mwh"],
                source=source,
            ))
            inserted += 1

    if new_rows:
        db.bulk_save_objects(new_rows)
    db.commit()

    return {
        "inserted": inserted,
        "updated": updated,
        "date_range_start": min_start.date().isoformat(),
        "date_range_end": max_start.date().isoformat(),
    }


def invalidate_cost_records(db: Session, start: datetime, end: datetime):
    """Deletes CostRecord rows in [start, end) so the next dashboard view
    rebuilds them fresh from the newly-uploaded usage data.

    This matters because of a real interaction with an earlier
    optimization: rebuild_cost_records has a fast path that skips
    recomputing CostRecord for any date range fully in the past, on the
    assumption that past costs "can't change" once computed. That's true
    for settlement prices alone, but false the moment real usage data can
    arrive after a date has already been viewed (and thus already cached
    with the notional estimate) — without this explicit invalidation, an
    upload for an already-viewed past date would silently have no visible
    effect until the affected CostRecord rows were cleared some other way."""
    db.execute(delete(CostRecord).where(CostRecord.interval_start >= start, CostRecord.interval_start < end))
    db.commit()
