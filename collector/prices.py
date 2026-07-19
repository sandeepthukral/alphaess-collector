"""Frank Energie market-price fetcher -> InfluxDB.

Fetches all-in electricity prices from Frank Energie's public GraphQL API and
writes them to the `market_price` measurement, for use by pricing.py (the
battery-savings analysis). See DESIGN-battery-savings.md.

The API returns one row per Frank billing interval (hourly in 2026), each with
the price broken into components that already include BTW:

    total = marketPrice + marketPriceTax + sourcingMarkupPrice + energyTaxPrice

`from`/`till` are UTC instants; the query's startDate is an Amsterdam *local*
date, so one call returns that local day's hourly rows (23/24/25 on DST days).
No authentication is required for market prices.

Run modes:
    python prices.py                      # fetch yesterday..tomorrow (local NL)
    python prices.py --date 2026-07-18    # one local day
    python prices.py --backfill 2026-01-01 2026-07-18   # inclusive range
    python prices.py --dry-run --date 2026-07-18        # print, no InfluxDB
"""

import argparse
import datetime as dt
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

FRANK_URL = os.environ.get(
    "FRANK_GRAPHQL_URL", "https://frank-graphql-prod.graphcdn.app/"
)
NL_TZ = ZoneInfo("Europe/Amsterdam")
MEASUREMENT = "market_price"

# Component fields as returned by the API -> our InfluxDB field names.
COMPONENTS = {
    "marketPrice": "market_price",
    "marketPriceTax": "market_price_tax",
    "sourcingMarkupPrice": "sourcing_markup",
    "energyTaxPrice": "energy_tax",
}

_QUERY = (
    "query MarketPrices($startDate: Date!, $endDate: Date!) {"
    " marketPricesElectricity(startDate: $startDate, endDate: $endDate) {"
    " from till marketPrice marketPriceTax sourcingMarkupPrice"
    " energyTaxPrice perUnit } }"
)

log = logging.getLogger("frank-prices")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def _parse_instant(value: str) -> dt.datetime:
    """Parse an API ISO timestamp (…Z) into an aware UTC datetime."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_prices_for_day(local_date: dt.date) -> list[dict]:
    """Fetch one Amsterdam local day's hourly prices.

    Returns a list of dicts with parsed floats plus `from`/`till` (aware UTC
    datetimes), `duration_s`, and `total`. Empty list if the API has no data
    for that day (e.g. a future day before day-ahead publication).
    """
    variables = {
        "startDate": local_date.isoformat(),
        "endDate": (local_date + dt.timedelta(days=1)).isoformat(),
    }
    resp = requests.post(
        FRANK_URL,
        json={"operationName": "MarketPrices", "query": _QUERY, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        messages = " ".join(e.get("message", "") for e in body["errors"])
        # A day with no published prices (e.g. tomorrow before the day-ahead
        # auction) comes back as an error, not empty data — treat as "no data".
        if "no marketprices found" in messages.lower():
            log.info("No prices published yet for %s", local_date)
            return []
        raise RuntimeError(f"GraphQL error for {local_date}: {body['errors']}")
    raw = (body.get("data") or {}).get("marketPricesElectricity") or []

    rows: list[dict] = []
    for r in raw:
        if r.get("perUnit") and r["perUnit"].upper() != "KWH":
            log.warning("Unexpected perUnit=%s for %s, skipping row", r["perUnit"], local_date)
            continue
        try:
            comps = {out: float(r[api]) for api, out in COMPONENTS.items()}
        except (KeyError, TypeError, ValueError):
            log.warning("Row missing/invalid price components for %s: %s", local_date, r)
            continue
        start = _parse_instant(r["from"])
        till = _parse_instant(r["till"])
        rows.append(
            {
                **comps,
                "total": round(sum(comps.values()), 6),
                "from": start,
                "till": till,
                "duration_s": (till - start).total_seconds(),
            }
        )
    return rows


def row_to_point(row: dict) -> Point:
    point = (
        Point(MEASUREMENT)
        .tag("source", "frank")
        .tag("unit", "kwh")
        .time(row["from"], WritePrecision.S)
    )
    for field in ("market_price", "market_price_tax", "sourcing_markup", "energy_tax", "total"):
        point = point.field(field, row[field])
    point = point.field("duration_s", float(row["duration_s"]))
    return point


def daterange(start: dt.date, end: dt.date):
    """Inclusive local-date range."""
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def run(days: list[dt.date], dry_run: bool) -> None:
    write_api = None
    client = None
    if not dry_run:
        client = InfluxDBClient(
            url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"), org=env("INFLUX_ORG")
        )
        write_api = client.write_api(write_options=SYNCHRONOUS)
        bucket = env("INFLUX_BUCKET")

    total_rows = 0
    try:
        for i, day in enumerate(days):
            if i:
                time.sleep(0.3)  # be polite to the API on multi-day backfills
            try:
                rows = fetch_prices_for_day(day)
            except Exception:
                log.exception("Failed to fetch prices for %s", day)
                continue
            if not rows:
                log.warning("No prices returned for %s (not yet published?)", day)
                continue
            if dry_run:
                span = f"{rows[0]['from'].isoformat()} .. {rows[-1]['till'].isoformat()}"
                log.info(
                    "%s: %d rows (%s), all-in %.5f..%.5f €/kWh",
                    day, len(rows), span,
                    min(r["total"] for r in rows), max(r["total"] for r in rows),
                )
            else:
                write_api.write(bucket=bucket, record=[row_to_point(r) for r in rows])
                log.info("%s: wrote %d price rows", day, len(rows))
            total_rows += len(rows)
    finally:
        if client:
            client.close()
    log.info("Done: %d price rows across %d day(s)%s",
             total_rows, len(days), " (dry-run, nothing written)" if dry_run else "")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch Frank Energie market prices into InfluxDB.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", metavar="YYYY-MM-DD", help="Fetch a single local (NL) day.")
    g.add_argument(
        "--backfill", nargs=2, metavar=("START", "END"),
        help="Fetch an inclusive range of local (NL) days.",
    )
    p.add_argument("--dry-run", action="store_true", help="Print results, do not write to InfluxDB.")
    return p.parse_args(argv)


def resolve_days(args: argparse.Namespace) -> list[dt.date]:
    if args.date:
        return [dt.date.fromisoformat(args.date)]
    if args.backfill:
        start, end = (dt.date.fromisoformat(x) for x in args.backfill)
        if start > end:
            log.error("backfill START (%s) is after END (%s)", start, end)
            sys.exit(1)
        return list(daterange(start, end))
    # Default: yesterday, today, tomorrow (local NL). Day-ahead prices for
    # tomorrow are usually published early afternoon; a missing day is skipped.
    today = dt.datetime.now(NL_TZ).date()
    return [today - dt.timedelta(days=1), today, today + dt.timedelta(days=1)]


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(sys.argv[1:])
    run(resolve_days(args), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
