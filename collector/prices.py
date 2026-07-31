"""Frank Energie market-price fetcher -> InfluxDB.

Fetches all-in electricity prices from Frank Energie's public GraphQL API and
writes them to the `market_price` measurement, for use by pricing.py (the
battery-savings analysis). See DESIGN-battery-savings.md.

The API returns one row per Frank billing interval -- hourly through
2026-07-31, 15-minute from 2026-08-01 under the new settlement contract --
each with the price broken into components that already include BTW. Slot
length is read from each row's own from/till, never assumed, so this needs no
code change across the cutover:

    total = marketPrice + marketPriceTax + sourcingMarkupPrice + energyTaxPrice

`from`/`till` are UTC instants; the query's startDate is an Amsterdam *local*
date, so one call returns that local day's rows -- 23/24/25 hourly through
2026-07-31, 92/96/100 quarter-hourly from 2026-08-01, across DST. No
authentication is required for market prices.

Run modes:
    python prices.py                      # fetch yesterday..tomorrow (local NL)
    python prices.py --date 2026-07-18    # one local day
    python prices.py --backfill 2026-01-01 2026-07-18   # inclusive range
    python prices.py --dry-run --date 2026-07-18        # print, no InfluxDB

Fallback for a cutover that doesn't fully land server-side: `--reconstruct-
if-coarse` reconstructs quarter-hour prices from EnergyZero's public day-
ahead feed for any on/after-2026-08-01 day where Frank's own rows are still
hourly. See DESIGN-battery-savings.md and CODE-REVIEW.md for why this is
opt-in rather than automatic.
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
ENERGYZERO_URL = os.environ.get(
    "ENERGYZERO_URL", "https://public.api.energyzero.nl/public/v1/prices"
)
NL_TZ = ZoneInfo("Europe/Amsterdam")
MEASUREMENT = "market_price"
CUTOVER_DATE = dt.date(2026, 8, 1)

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
    """Fetch one Amsterdam local day's per-slot prices (hourly through
    2026-07-31, 15-minute from 2026-08-01).

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


def row_to_point(row: dict, source: str = "frank") -> Point:
    point = (
        Point(MEASUREMENT)
        .tag("source", source)
        .tag("unit", "kwh")
        .time(row["from"], WritePrecision.S)
    )
    for field in ("market_price", "market_price_tax", "sourcing_markup", "energy_tax", "total"):
        point = point.field(field, row[field])
    point = point.field("duration_s", float(row["duration_s"]))
    return point


def fetch_quarter_hour_wholesale(local_date: dt.date) -> list[dict]:
    """Fetch one Amsterdam local day's real quarter-hour day-ahead wholesale
    prices from EnergyZero's public API (no auth) -- the same free feed the
    sibling battery-planning repo already relies on. This is the NL day-ahead
    auction price, which has cleared in 15-minute MTUs since 2025-10-01 --
    independent of, and available well ahead of, Frank Energie's own billing
    granularity.

    Returns a list of dicts: {"from", "till" (aware UTC datetimes),
    "wholesale_price" (EUR/kWh)}. Empty list on any fetch/parse problem --
    callers must treat that as "no fallback data", not as zero-priced hours.
    """
    resp = requests.get(
        ENERGYZERO_URL,
        params={
            "date": local_date.strftime("%d-%m-%Y"),
            "interval": "INTERVAL_QUARTER",
            "energyType": "ENERGY_TYPE_ELECTRICITY",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    quarters: list[dict] = []
    for entry in body.get("base") or []:
        try:
            start = _parse_instant(entry["start"])
            till = _parse_instant(entry["end"])
            price = float(entry["price"]["value"])
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping unparseable EnergyZero row for %s: %s", local_date, entry)
            continue
        quarters.append({"from": start, "till": till, "wholesale_price": price})
    quarters.sort(key=lambda q: q["from"])
    return quarters


def reconstruct_quarter_hour_rows(hourly_rows: list[dict], wholesale_quarters: list[dict]) -> list[dict]:
    """Reconstruct quarter-hour price rows from coarse (hourly-or-larger)
    Frank rows, using EnergyZero's real quarter-hour wholesale prices for the
    shape within each row and Frank's own row for everything else.

    Empirically verified (see DESIGN-battery-savings.md): Frank's
    `market_price` is the plain average of the real quarter-hour wholesale
    prices it spans, and `market_price_tax` is exactly 21% (BTW) of
    `market_price` -- so both are re-derived per quarter rather than split
    evenly, while `sourcing_markup`/`energy_tax` (flat per-kWh charges) carry
    over unchanged.

    A row whose span doesn't contain any matching wholesale quarters (an
    EnergyZero gap/outage, or bad input) is returned unchanged rather than
    guessed -- this keeps the existing price_coverage gate as the real
    backstop: a skipped row shows up as coarse/missing, not silently wrong.
    """
    out: list[dict] = []
    for row in hourly_rows:
        contained = [
            q for q in wholesale_quarters
            if q["from"] >= row["from"] and q["till"] <= row["till"]
        ]
        expected = round(row["duration_s"] / 900)
        if not contained or len(contained) != expected:
            log.warning(
                "Not reconstructing %s..%s: expected %d quarter(s) of wholesale data, found %d",
                row["from"], row["till"], expected, len(contained),
            )
            out.append(row)
            continue
        btw_ratio = row["market_price_tax"] / row["market_price"] if row["market_price"] else 0.0
        for q in contained:
            market_price = q["wholesale_price"]
            market_price_tax = market_price * btw_ratio
            sourcing_markup = row["sourcing_markup"]
            energy_tax = row["energy_tax"]
            out.append({
                "market_price": market_price,
                "market_price_tax": market_price_tax,
                "sourcing_markup": sourcing_markup,
                "energy_tax": energy_tax,
                "total": round(market_price + market_price_tax + sourcing_markup + energy_tax, 6),
                "from": q["from"],
                "till": q["till"],
                "duration_s": (q["till"] - q["from"]).total_seconds(),
            })
    return out


def daterange(start: dt.date, end: dt.date):
    """Inclusive local-date range."""
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def run(days: list[dt.date], dry_run: bool, reconstruct_if_coarse: bool = False) -> None:
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

            # Only genuinely coarse rows are candidates for reconstruction --
            # a mixed day (e.g. right at the cutover boundary) must not have
            # already-native quarter-hour rows re-derived too.
            fine_rows = [r for r in rows if r["duration_s"] < 3600]
            coarse_rows = [r for r in rows if r["duration_s"] >= 3600]
            tagged: list[tuple[dict, str]] = [(r, "frank") for r in fine_rows]

            if day >= CUTOVER_DATE and coarse_rows:
                log.warning(
                    "Frank still returning hourly-or-coarser rows for %s after the "
                    "15-min cutover -- run with --reconstruct-if-coarse to backfill "
                    "quarter-hour prices from EnergyZero.", day,
                )
                reconstructed = None
                if reconstruct_if_coarse:
                    wholesale = fetch_quarter_hour_wholesale(day)
                    if wholesale:
                        reconstructed = reconstruct_quarter_hour_rows(coarse_rows, wholesale)
                    else:
                        log.warning(
                            "No EnergyZero wholesale data for %s -- leaving coarse rows as-is",
                            day,
                        )
                if reconstructed is not None:
                    tagged += [(r, "frank+energyzero") for r in reconstructed]
                else:
                    tagged += [(r, "frank") for r in coarse_rows]
            else:
                tagged += [(r, "frank") for r in coarse_rows]

            tagged.sort(key=lambda pair: pair[0]["from"])
            rows = [r for r, _ in tagged]

            if dry_run:
                span = f"{rows[0]['from'].isoformat()} .. {rows[-1]['till'].isoformat()}"
                sources = sorted({s for _, s in tagged})
                log.info(
                    "%s: %d rows (%s), all-in %.5f..%.5f €/kWh, source=%s",
                    day, len(rows), span,
                    min(r["total"] for r in rows), max(r["total"] for r in rows),
                    "+".join(sources),
                )
            else:
                write_api.write(
                    bucket=bucket,
                    record=[row_to_point(r, source=s) for r, s in tagged],
                )
                sources = sorted({s for _, s in tagged})
                log.info("%s: wrote %d price rows (source=%s)", day, len(rows), "+".join(sources))
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
    p.add_argument(
        "--reconstruct-if-coarse", action="store_true",
        help=(
            "For days on/after the 15-min cutover where Frank's own rows are still "
            "hourly-or-coarser, reconstruct quarter-hour prices from EnergyZero's "
            "public day-ahead feed instead. Opt-in: see DESIGN-battery-savings.md."
        ),
    )
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
    run(resolve_days(args), dry_run=args.dry_run, reconstruct_if_coarse=args.reconstruct_if_coarse)


if __name__ == "__main__":
    main()
