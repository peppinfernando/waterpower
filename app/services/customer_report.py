"""Parses a per-customer MPRN meter-data export (the format Irish network
operators issue — a short metadata header block, then a Date/Time/Value/
Status table of interval readings) and builds a day-by-day report pairing
real consumption against real I-SEM wholesale settlement prices.

Why day-level, not month-level or interval-level: the frontend needs to
support three ways of looking at the data — a single month, a custom
date range, or all months combined — and recomputing from scratch for
each would mean either re-uploading the file or the backend holding
per-request state. Returning one row per day (a few hundred rows for
9 months, trivially small) lets the frontend aggregate whichever period
is selected itself, instantly, including re-running the tariff
calculation live as the user edits network charge / margin / VAT —
without a server round-trip for every edit.

Why day-level, not interval-level: shipping ~26,000 raw 15-minute rows
to the browser just to re-aggregate them client-side would be wasteful;
the band breakdown (night/day/peak hours and cost) is pre-computed once
per day server-side instead, which is all any of the three view modes
actually need.
"""
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import openpyxl
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SettlementPrice
from app.services.tariff import DEFAULT_BANDS, _in_band

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # a year of 15-min data is still only a few MB
INTERVAL_HOURS = 0.25  # 15 minutes, as a fraction of an hour — for kW -> kWh


def _find_header_row(ws, max_scan_rows: int = 20) -> Optional[int]:
    """Scans the first few rows for the one starting Date/Time/Value/...,
    rather than assuming a fixed row number — the metadata block above it
    (Profile Number, MPRN, UoM, etc.) isn't guaranteed to be exactly 5
    rows on every export."""
    for row_idx in range(1, max_scan_rows + 1):
        row = ws[row_idx]
        values = [str(c.value).strip().lower() if c.value is not None else "" for c in row[:4]]
        if values[:2] == ["date", "time"]:
            return row_idx
    return None


def _extract_metadata(ws, header_row: int) -> Dict[str, str]:
    """Pulls MPRN / Profile Description / UoM from whatever label:value
    pairs appear in the rows above the data header — scanning for the
    label text rather than a fixed cell address, since its column
    position isn't guaranteed either."""
    wanted = {"mprn": None, "profile description": None, "uom": None, "profile number": None}
    for row_idx in range(1, header_row):
        cells = [c.value for c in ws[row_idx]]
        for i, cell in enumerate(cells):
            if cell is None:
                continue
            key = str(cell).strip().lower()
            if key in wanted and i + 1 < len(cells) and cells[i + 1] is not None:
                wanted[key] = str(cells[i + 1]).strip()
    return {
        "mprn": wanted["mprn"],
        "profile_description": wanted["profile description"],
        "uom": wanted["uom"],
        "profile_number": wanted["profile number"],
    }


def parse_mprn_usage_xlsx(file_bytes: bytes) -> Tuple[List[dict], Dict[str, str], List[str], List[str]]:
    """Returns (records, metadata, warnings, errors). records is [] if
    errors is non-empty. Each record is {interval_start: datetime,
    volume_kwh: float}."""
    warnings: List[str] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
    except Exception as e:
        return [], {}, [], [f"Could not read this as an Excel file ({e})."]

    header_row = _find_header_row(ws)
    if header_row is None:
        return [], {}, [], [
            "Couldn't find the Date/Time header row in the first 20 rows. "
            "Expected a table with columns Date, Time, Value, Status."
        ]

    metadata = _extract_metadata(ws, header_row)
    wb.close()

    try:
        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=0, skiprows=header_row - 1)
    except Exception as e:
        return [], metadata, [], [f"Found the header row but couldn't read the data table ({e})."]

    df.columns = [str(c).strip() for c in df.columns]
    missing_cols = [c for c in ["Date", "Time", "Value"] if c not in df.columns]
    if missing_cols:
        return [], metadata, [], [f"Missing expected column(s): {', '.join(missing_cols)}."]

    # UoM tells us whether Value is already energy (kWh) or power (kW) that
    # needs multiplying by the interval length to become energy. Default to
    # treating it as power (the observed format) if UoM is missing or
    # unrecognised, but say so — silently guessing wrong here would just
    # quietly overstate or understate consumption by 4x.
    uom = (metadata.get("uom") or "").strip().upper()
    if uom == "KWH":
        is_energy_already = True
    elif uom == "KW":
        is_energy_already = False
    else:
        is_energy_already = False
        warnings.append(f"Unrecognised or missing unit (UoM='{metadata.get('uom')}') — assumed kW (power), converted to kWh using a 15-minute interval. Verify this against the source file.")

    ts = pd.to_datetime(df["Date"].astype(str) + " " + df["Time"].astype(str), dayfirst=True, errors="coerce")
    vals = pd.to_numeric(df["Value"], errors="coerce")

    records = []
    skipped = 0
    for i in range(len(df)):
        t, v = ts.iloc[i], vals.iloc[i]
        if pd.isna(t) or pd.isna(v):
            skipped += 1
            continue
        volume_kwh = float(v) if is_energy_already else float(v) * INTERVAL_HOURS
        if volume_kwh < 0:
            skipped += 1
            continue
        records.append({"interval_start": t.to_pydatetime().replace(tzinfo=None), "volume_kwh": volume_kwh})

    if skipped:
        warnings.append(f"Skipped {skipped} row(s) with an unreadable timestamp, missing value, or negative value.")
    if not records:
        return [], metadata, warnings, ["No valid usage rows could be parsed from this file."]

    records.sort(key=lambda r: r["interval_start"])
    return records, metadata, warnings, []


def _floor_to_half_hour(dt: datetime) -> datetime:
    """Maps a 15-minute reading to the 30-minute settlement interval it
    falls inside — HH:15 and HH:45 both belong to the half-hour that
    started at HH:00 / HH:30 respectively."""
    floored_minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


def build_customer_report(db: Session, records: List[dict]) -> Tuple[List[dict], List[str]]:
    """Matches usage records against real settlement prices and rolls up
    to one row per day, with a night/day/peak band breakdown on each.
    Returns (days, warnings) — warnings flag any intervals with no
    matching settlement price (excluded from cost, but not from the
    consumption total, since the meter reading is still real)."""
    if not records:
        return [], []

    min_dt = min(r["interval_start"] for r in records)
    max_dt = max(r["interval_start"] for r in records) + timedelta(minutes=30)

    price_rows = db.scalars(
        select(SettlementPrice).where(
            SettlementPrice.interval_start >= _floor_to_half_hour(min_dt),
            SettlementPrice.interval_start < max_dt,
        )
    ).all()
    price_by_half_hour = {p.interval_start: p.price_eur_per_mwh for p in price_rows}

    days: Dict[str, dict] = {}
    missing_price_count = 0

    for r in records:
        day_key = r["interval_start"].date().isoformat()
        day = days.setdefault(day_key, {
            "date": day_key,
            "consumption_kwh": 0.0,
            "buying_cost_eur": 0.0,
            "priced_consumption_kwh": 0.0,  # denominator for the day's avg price — excludes unpriced intervals
            "bands": {b["name"]: {"hours": 0.0, "consumption_kwh": 0.0, "buying_cost_eur": 0.0} for b in DEFAULT_BANDS},
            "half_hours": {},  # "HH:MM" -> {consumption_kwh, price_eur_per_mwh, buying_cost_eur} — the Daily view's detail
        })

        day["consumption_kwh"] += r["volume_kwh"]

        band = next((b for b in DEFAULT_BANDS if _in_band(r["interval_start"].hour, b)), None)
        if band:
            day["bands"][band["name"]]["hours"] += 0.25
            day["bands"][band["name"]]["consumption_kwh"] += r["volume_kwh"]

        hh_start = _floor_to_half_hour(r["interval_start"])
        hh_key = hh_start.strftime("%H:%M")
        hh = day["half_hours"].setdefault(hh_key, {"consumption_kwh": 0.0, "price_eur_per_mwh": None, "buying_cost_eur": 0.0})
        hh["consumption_kwh"] += r["volume_kwh"]

        price = price_by_half_hour.get(hh_start)
        if price is None:
            missing_price_count += 1
            continue
        cost = r["volume_kwh"] * price / 1000.0  # price is EUR/MWh; volume is kWh
        day["buying_cost_eur"] += cost
        day["priced_consumption_kwh"] += r["volume_kwh"]
        hh["price_eur_per_mwh"] = price
        hh["buying_cost_eur"] += cost
        if band:
            day["bands"][band["name"]]["buying_cost_eur"] += cost

    warnings = []
    if missing_price_count:
        total = len(records)
        warnings.append(
            f"{missing_price_count} of {total} usage interval(s) had no matching wholesale settlement price "
            f"(likely outside the range currently loaded in this system) — excluded from cost totals, "
            f"but still counted in consumption totals."
        )

    result = []
    for day_key in sorted(days):
        d = days[day_key]
        avg_price = (d["buying_cost_eur"] / d["priced_consumption_kwh"] * 1000.0) if d["priced_consumption_kwh"] else None
        result.append({
            "date": d["date"],
            "consumption_kwh": round(d["consumption_kwh"], 3),
            "buying_cost_eur": round(d["buying_cost_eur"], 4),
            "avg_wholesale_price_eur_per_mwh": round(avg_price, 2) if avg_price is not None else None,
            "bands": {
                name: {
                    "hours": round(b["hours"], 2),
                    "consumption_kwh": round(b["consumption_kwh"], 3),
                    "buying_cost_eur": round(b["buying_cost_eur"], 4),
                } for name, b in d["bands"].items()
            },
            "half_hours": [
                {
                    "time": hh_key,
                    "consumption_kwh": round(hh["consumption_kwh"], 3),
                    "price_eur_per_mwh": round(hh["price_eur_per_mwh"], 2) if hh["price_eur_per_mwh"] is not None else None,
                    "buying_cost_eur": round(hh["buying_cost_eur"], 4),
                }
                for hh_key, hh in sorted(d["half_hours"].items())
            ],
        })
    return result, warnings
