"""SEM-O Balancing Market settlement price ingestion.

The public sem-o.com site is a JS single-page app — price data isn't in the
HTML. SEM-O actually publishes this data two ways:

1. Static Reports library (sem-o.com/market-data/static-reports) — flat
   CSV/XML files per settlement period, historically the most reliable way
   to bulk-download Imbalance Settlement Price data.
2. Dynamic Reports (sem-o.com/market-data/dynamic-reports) — an interactive
   charting tool backed by a REST API that typically needs a report ID and,
   for some feeds, a registered market-data account.

Neither exposes a documented public endpoint without registering on the
SEM-O portal. Rather than block the whole platform on that registration,
this module defines a stable interface and swaps implementations via the
SEMO_CLIENT env var:

    SEMO_CLIENT=mock            -> synthetic data, works today
    SEMO_CLIENT=static_report   -> stub for the real static report; fill in
                                    REPORT_URL below once you have portal access

Everything downstream (database, aggregation, API) only depends on
`fetch_settlement_prices` returning a list of PricePoint — swapping the
implementation never touches the rest of the app.
"""
import os
import random
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Protocol

import httpx


@dataclass
class PricePoint:
    interval_start: datetime  # UTC, start of the 30-min settlement period
    interval_minutes: int
    price_eur_per_mwh: float
    net_imbalance_volume_mwh: Optional[float] = None


class SemOClient(Protocol):
    def fetch_settlement_prices(self, start: datetime, end: datetime) -> List[PricePoint]:
        ...


class MockSemOClient:
    """Deterministic synthetic BM prices with a realistic daily shape:
    low overnight, morning ramp, evening peak — plus some noise, so the
    dashboard and tariff logic have something believable to work with
    before the real feed is wired up."""

    def fetch_settlement_prices(self, start: datetime, end: datetime) -> List[PricePoint]:
        points = []
        t = start
        rng = random.Random(int(start.timestamp()))  # deterministic per day
        while t < end:
            hour = t.hour + t.minute / 60
            # base daily curve: trough ~4am, peak ~18:00
            base = 60 + 55 * math.exp(-((hour - 18) ** 2) / 8) + 15 * math.sin(hour / 24 * 2 * math.pi)
            noise = rng.uniform(-8, 8)
            price = round(max(5.0, base + noise), 2)
            niv = round(rng.uniform(-80, 80), 1)
            points.append(PricePoint(
                interval_start=t,
                interval_minutes=30,
                price_eur_per_mwh=price,
                net_imbalance_volume_mwh=niv,
            ))
            t += timedelta(minutes=30)
        return points


class StaticReportSemOClient:
    """Stub for the real SEM-O static report feed.

    Fill in REPORT_URL once confirmed via the SEM-O market-data portal
    (Market Data > Static Reports > Historic Market Data, or the Dynamic
    Reports API report ID for Imbalance Settlement Price). The static
    reports are typically CSV with columns similar to:
        Period, Trade Date/Start Time, Trade Date/End Time,
        Imbalance Settlement Price, Net Imbalance Volume

    This implementation downloads and parses that CSV shape. Adjust the
    column mapping below once you see the real file.
    """

    REPORT_URL = os.getenv(
        "SEMO_REPORT_URL",
        "https://www.sem-o.com/PLACEHOLDER-fill-in-real-static-report-url",
    )

    def fetch_settlement_prices(self, start: datetime, end: datetime) -> List[PricePoint]:
        with httpx.Client(timeout=30) as client:
            resp = client.get(self.REPORT_URL, params={
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
            })
            resp.raise_for_status()
            return self._parse_csv(resp.text)

    @staticmethod
    def _parse_csv(csv_text: str) -> List[PricePoint]:
        import csv
        import io

        points = []
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            # NOTE: adjust these column names to match the real SEM-O export
            start_str = row.get("Trade Date / Start Time") or row["interval_start"]
            price_str = (row.get("Imbalance Settlement Price") or row["price"]).replace("€", "").strip()
            niv_str = row.get("Net Imbalance Volume")
            points.append(PricePoint(
                interval_start=datetime.strptime(start_str, "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc),
                interval_minutes=30,
                price_eur_per_mwh=float(price_str),
                net_imbalance_volume_mwh=float(niv_str) if niv_str else None,
            ))
        return points


def get_client() -> SemOClient:
    kind = os.getenv("SEMO_CLIENT", "mock")
    if kind == "static_report":
        return StaticReportSemOClient()
    return MockSemOClient()
