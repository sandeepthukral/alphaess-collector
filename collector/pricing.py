"""Battery-savings pricing: per-day cost of two worlds -> InfluxDB.

For each complete local (NL) day, integrates the 30 s power samples against
Frank Energie's hourly prices and computes:

  Model 1 (with battery, actual)  - price the real grid flows.
  Model 2 (no battery, counterfactual) - grid_cf = grid + battery, priced the
      same way (whatever the battery charged would have been exported, whatever
      it discharged would have been imported).

Battery value = cost(Model 2) - cost(Model 1). Results go to the `daily_cost`
measurement. See DESIGN-battery-savings.md for the full rationale (including why
per-hour netting is exact for 2026 saldering).

Run modes:
    python pricing.py --date 2026-07-17          # one local day, InfluxDB I/O
    python pricing.py --backfill 2026-07-17 2026-07-31
    python pricing.py --date 2026-07-17 --dry-run # compute + print, no write
    python pricing.py --csv power_2026-07-17.csv --date 2026-07-17 --dry-run
        # validate against an exported CSV; prices fetched live from Frank.
"""

import argparse
import bisect
import csv
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from prices import NL_TZ, fetch_prices_for_day

POWER_MEASUREMENT = "power_readings"
DAILY_MEASUREMENT = "daily_cost"

# Bump when the model or stored schema changes; days computed at an older
# version are reprocessed rather than skipped.
MODEL_VERSION = "1"

# Complete-day gate.
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
MIN_COVERAGE = float(os.environ.get("PRICING_MIN_COVERAGE", "0.98"))
MAX_GAP_S = float(os.environ.get("PRICING_MAX_GAP_S", "1200"))  # 20 min

# Optional: convert ΔSoC% to kWh for the borrow/bank indicator.
BATTERY_CAPACITY_KWH = os.environ.get("BATTERY_CAPACITY_KWH")

log = logging.getLogger("pricing")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


@dataclass
class Sample:
    time: dt.datetime  # aware UTC
    pv: float
    grid: float  # + import, - export
    load: float
    battery: float  # + discharge, - charge
    soc: float


# --------------------------------------------------------------------------
# Integration
# --------------------------------------------------------------------------

def _accumulate(bucket: list, dt_h: float, ps: float, pe: float) -> None:
    """Add the energy of a linear power ramp ps->pe over dt_h hours into
    [import_wh, export_wh], splitting at a zero crossing if the sign flips."""
    if dt_h <= 0:
        return
    if (ps >= 0) == (pe >= 0):  # no sign change
        wh = (ps + pe) / 2 * dt_h
        if wh >= 0:
            bucket[0] += wh
        else:
            bucket[1] += -wh
        return
    # Sign change: find zero crossing fraction, split into two triangles.
    f = ps / (ps - pe)  # in (0, 1)
    wh_first = ps * (f * dt_h) / 2
    wh_second = pe * ((1 - f) * dt_h) / 2
    for wh in (wh_first, wh_second):
        if wh >= 0:
            bucket[0] += wh
        else:
            bucket[1] += -wh


def integrate_by_interval(samples: list[Sample], power_fn, intervals: list[dict]):
    """Integrate a power signal (W) into per-interval (import_wh, export_wh).

    `power_fn(sample) -> float`. Segments between samples are treated as linear
    ramps (trapezoidal), split at every interval boundary and at zero crossings
    so import and export are separated correctly even within one hourly slot.
    """
    froms = [iv["from"] for iv in intervals]
    tills = [iv["till"] for iv in intervals]
    boundaries = sorted(set(froms) | set(tills))
    result = [[0.0, 0.0] for _ in intervals]

    def interval_index(t: dt.datetime):
        i = bisect.bisect_right(froms, t) - 1
        if 0 <= i < len(intervals) and froms[i] <= t < tills[i]:
            return i
        return None

    for a, b in zip(samples, samples[1:]):
        t0, t1 = a.time, b.time
        span = (t1 - t0).total_seconds()
        if span <= 0:
            continue
        p0, p1 = power_fn(a), power_fn(b)

        def interp(t):
            return p0 + (p1 - p0) * ((t - t0).total_seconds() / span)

        cuts = [t for t in boundaries if t0 < t < t1]
        points = [t0, *cuts, t1]
        for s, e in zip(points, points[1:]):
            idx = interval_index(s + (e - s) / 2)
            if idx is None:
                continue  # sample outside any known price interval
            _accumulate(result[idx], (e - s).total_seconds() / 3600.0, interp(s), interp(e))
    return result


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

def import_price(iv: dict) -> float:
    """All-in consumption price (€/kWh)."""
    return iv["total"]


def export_price(iv: dict) -> float:
    """Salded feed-in price for 2026 (€/kWh).

    Option (b) from DESIGN-battery-savings.md: commodity credited per-slot with
    the sourcing markup deducted, energy tax refunded under saldering, BTW kept.
    The ~15% teruglever bonus is intentionally excluded. Components are already
    BTW-inclusive. Pin against a real teruglevering bill line post-2026-07-26.
    """
    return iv["market_price"] + iv["market_price_tax"] - iv["sourcing_markup"] + iv["energy_tax"]


def compute_day(samples: list[Sample], intervals: list[dict], day: dt.date) -> dict:
    """Compute the daily_cost fields from samples + price intervals."""
    actual = integrate_by_interval(samples, lambda s: s.grid, intervals)
    counterfactual = integrate_by_interval(samples, lambda s: s.grid + s.battery, intervals)

    cost1 = cost2 = 0.0
    imp1 = exp1 = imp2 = exp2 = 0.0  # kWh totals
    for iv, (ia, ea), (ic, ec) in zip(intervals, actual, counterfactual):
        pi, pe = import_price(iv), export_price(iv)
        ia, ea, ic, ec = ia / 1000, ea / 1000, ic / 1000, ec / 1000  # Wh -> kWh
        cost1 += ia * pi - ea * pe
        cost2 += ic * pi - ec * pe
        imp1 += ia; exp1 += ea; imp2 += ic; exp2 += ec

    # Data-quality metrics. Coverage is time-based: normal cadence drift and a
    # skipped poll or two never count as missing; only real outages (gaps beyond
    # 3x the poll interval) plus any un-sampled head/tail of the day do.
    win_start, win_end = day_window_utc(day)
    day_len = (win_end - win_start).total_seconds()
    gaps = [(b.time - a.time).total_seconds() for a, b in zip(samples, samples[1:])]
    max_gap = max(gaps) if gaps else 0.0
    head = max(0.0, (samples[0].time - win_start).total_seconds())
    tail = max(0.0, (win_end - samples[-1].time).total_seconds())
    outage = sum(g - POLL_INTERVAL_S for g in gaps if g > 3 * POLL_INTERVAL_S)
    coverage = max(0.0, 1.0 - (head + tail + outage) / day_len) if day_len else 0.0
    span_s = (samples[-1].time - samples[0].time).total_seconds()

    # Energy-balance residual (kWh of |pv + grid + battery - load| integrated).
    residual = 0.0
    for a, b in zip(samples, samples[1:]):
        ra = a.pv + a.grid + a.battery - a.load
        rb = b.pv + b.grid + b.battery - b.load
        residual += (abs(ra) + abs(rb)) / 2 * ((b.time - a.time).total_seconds() / 3600.0)
    residual /= 1000

    result = {
        "cost_model1": round(cost1, 5),
        "cost_model2": round(cost2, 5),
        "saving": round(cost2 - cost1, 5),
        "import_kwh_actual": round(imp1, 4),
        "export_kwh_actual": round(exp1, 4),
        "import_kwh_cf": round(imp2, 4),
        "export_kwh_cf": round(exp2, 4),
        "delta_soc_percent": round(samples[-1].soc - samples[0].soc, 2),
        "balance_residual_kwh": round(residual, 4),
        "coverage": round(coverage, 4),
        "max_gap_s": round(max_gap, 1),
        "sample_count": len(samples),
        "span_s": round(span_s, 1),
    }
    if BATTERY_CAPACITY_KWH:
        result["delta_soc_kwh"] = round(
            result["delta_soc_percent"] / 100 * float(BATTERY_CAPACITY_KWH), 4
        )
    return result


def day_window_utc(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time(), NL_TZ).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), NL_TZ).astimezone(dt.timezone.utc)
    return start, end


def gate(result: dict) -> tuple[bool, str]:
    if result["coverage"] < MIN_COVERAGE:
        return False, f"coverage {result['coverage']:.3f} < {MIN_COVERAGE}"
    if result["max_gap_s"] > MAX_GAP_S:
        return False, f"max gap {result['max_gap_s']:.0f}s > {MAX_GAP_S:.0f}s"
    return True, "ok"


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_samples_csv(path: str, start: dt.datetime, end: dt.datetime) -> list[Sample]:
    """Load samples from an InfluxDB CSV export (annotated or plain), pivoted so
    each row has _time plus the five power fields. Rows are filtered to
    [start, end) and sorted by time."""
    samples: list[Sample] = []
    with open(path, newline="") as fh:
        rows = [r for r in csv.reader(fh) if r and not r[0].startswith("#")]
    if not rows:
        return samples
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    required = ["_time", "pv_power_w", "grid_power_w", "load_power_w", "battery_power_w", "soc_percent"]
    missing = [c for c in required if c not in idx]
    if missing:
        raise ValueError(f"CSV missing columns {missing}; header={header}")
    for r in rows[1:]:
        try:
            t = _parse_time(r[idx["_time"]])
        except (ValueError, IndexError):
            continue
        if not (start <= t < end):
            continue
        try:
            samples.append(Sample(
                time=t,
                pv=float(r[idx["pv_power_w"]]),
                grid=float(r[idx["grid_power_w"]]),
                load=float(r[idx["load_power_w"]]),
                battery=float(r[idx["battery_power_w"]]),
                soc=float(r[idx["soc_percent"]]),
            ))
        except (ValueError, IndexError):
            continue
    samples.sort(key=lambda s: s.time)
    return samples


_SAMPLE_FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{meas}" and r.sys_sn == "{sys_sn}")
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"])
"""


def load_samples_influx(query_api, bucket, sys_sn, start, end) -> list[Sample]:
    flux = _SAMPLE_FLUX.format(
        bucket=bucket, meas=POWER_MEASUREMENT, sys_sn=sys_sn,
        start=start.isoformat(), stop=end.isoformat(),
    )
    samples: list[Sample] = []
    for table in query_api.query(flux):
        for rec in table.records:
            v = rec.values
            try:
                samples.append(Sample(
                    time=rec.get_time(),
                    pv=float(v["pv_power_w"]), grid=float(v["grid_power_w"]),
                    load=float(v["load_power_w"]), battery=float(v["battery_power_w"]),
                    soc=float(v["soc_percent"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
    samples.sort(key=lambda s: s.time)
    return samples


_PRICE_FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "market_price")
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"])
"""


def load_prices_influx(query_api, bucket, start, end) -> list[dict]:
    flux = _PRICE_FLUX.format(bucket=bucket, start=start.isoformat(), stop=end.isoformat())
    intervals: list[dict] = []
    for table in query_api.query(flux):
        for rec in table.records:
            v = rec.values
            frm = rec.get_time()
            intervals.append({
                "from": frm,
                "till": frm + dt.timedelta(seconds=float(v["duration_s"])),
                "market_price": float(v["market_price"]),
                "market_price_tax": float(v["market_price_tax"]),
                "sourcing_markup": float(v["sourcing_markup"]),
                "energy_tax": float(v["energy_tax"]),
                "total": float(v["total"]),
            })
    intervals.sort(key=lambda iv: iv["from"])
    return intervals


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def process_day(day, samples, intervals, dry_run, write_ctx) -> None:
    if not samples:
        log.warning("%s: no power samples, skipping", day)
        return
    if not intervals:
        log.warning("%s: no prices available, skipping", day)
        return

    result = compute_day(samples, intervals, day)
    ok, why = gate(result)
    quality = (f"coverage={result['coverage']:.3f} max_gap={result['max_gap_s']:.0f}s "
               f"residual={result['balance_residual_kwh']:.3f}kWh")
    if not ok:
        log.warning("%s: EXCLUDED (%s) [%s]", day, why, quality)
        return

    log.info(
        "%s: Model1 €%.4f  Model2 €%.4f  saving €%.4f  "
        "(imp/exp actual %.2f/%.2f kWh, cf %.2f/%.2f kWh) [%s]",
        day, result["cost_model1"], result["cost_model2"], result["saving"],
        result["import_kwh_actual"], result["export_kwh_actual"],
        result["import_kwh_cf"], result["export_kwh_cf"], quality,
    )

    if dry_run:
        return
    write_api, bucket, sys_sn = write_ctx
    point = (
        Point(DAILY_MEASUREMENT)
        .tag("sys_sn", sys_sn)
        .tag("model_version", MODEL_VERSION)
        .time(day_window_utc(day)[0], WritePrecision.S)
    )
    for k, val in result.items():
        if val is not None:
            point = point.field(k, float(val))
    write_api.write(bucket=bucket, record=point)
    log.info("%s: wrote %s", day, DAILY_MEASUREMENT)


def run_csv(csv_path: str, days: list[dt.date]) -> None:
    """Offline validation path: samples from CSV, prices fetched live."""
    for day in days:
        start, end = day_window_utc(day)
        samples = load_samples_csv(csv_path, start, end)
        intervals = fetch_prices_for_day(day)
        process_day(day, samples, intervals, dry_run=True, write_ctx=None)


def run_influx(days: list[dt.date], dry_run: bool, force: bool) -> None:
    client = InfluxDBClient(url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"), org=env("INFLUX_ORG"))
    bucket = env("INFLUX_BUCKET")
    sys_sn = env("ALPHAESS_SYS_SN")
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    try:
        for day in days:
            if not force and not dry_run and _already_done(query_api, bucket, sys_sn, day):
                log.info("%s: already processed at model_version=%s, skipping", day, MODEL_VERSION)
                continue
            start, end = day_window_utc(day)
            samples = load_samples_influx(query_api, bucket, sys_sn, start, end)
            intervals = load_prices_influx(query_api, bucket, start, end)
            process_day(day, samples, intervals, dry_run, (write_api, bucket, sys_sn))
    finally:
        client.close()


def _already_done(query_api, bucket, sys_sn, day) -> bool:
    start, end = day_window_utc(day)
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {start.isoformat()}, stop: {end.isoformat()})
  |> filter(fn: (r) => r._measurement == "{DAILY_MEASUREMENT}"
        and r.sys_sn == "{sys_sn}" and r.model_version == "{MODEL_VERSION}")
  |> limit(n:1)
'''
    for table in query_api.query(flux):
        if table.records:
            return True
    return False


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def resolve_days(args) -> list[dt.date]:
    if args.date:
        return [dt.date.fromisoformat(args.date)]
    if args.backfill:
        start, end = (dt.date.fromisoformat(x) for x in args.backfill)
        if start > end:
            log.error("backfill START after END")
            sys.exit(1)
        return list(daterange(start, end))
    # Default: yesterday (the most recent complete local day).
    return [dt.datetime.now(NL_TZ).date() - dt.timedelta(days=1)]


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Compute per-day battery savings into InfluxDB.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", metavar="YYYY-MM-DD")
    g.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    p.add_argument("--csv", metavar="PATH", help="Read samples from a CSV export; prices fetched live; implies dry-run.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Reprocess even if already done.")
    args = p.parse_args()

    days = resolve_days(args)
    if args.csv:
        if not (args.date or args.backfill):
            log.error("--csv requires --date or --backfill to select the day(s)")
            sys.exit(1)
        run_csv(args.csv, days)
    else:
        run_influx(days, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
