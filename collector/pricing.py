"""Battery-savings pricing: per-day cost of two worlds -> InfluxDB.

For each complete local (NL) day, integrates the 30 s power samples against
Frank Energie's per-slot prices (hourly through 2026-07-31, 15-minute from
2026-08-01 -- slot length is read from each interval's own from/till, never
assumed) and computes:

  Model 1 (with battery, actual)  - price the real grid flows.
  Model 2 (no battery, counterfactual) - grid_cf = grid + battery, priced the
      same way (whatever the battery charged would have been exported, whatever
      it discharged would have been imported).

Battery value = cost(Model 2) - cost(Model 1). Results go to the `daily_cost`
measurement. See DESIGN-battery-savings.md for the full rationale (including why
per-slot netting is exact for 2026 saldering, independent of slot length).

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
import time
from dataclasses import dataclass

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from prices import NL_TZ, fetch_prices_for_day

POWER_MEASUREMENT = "power_readings"
DAILY_MEASUREMENT = "daily_cost"

# Bump when the model or stored schema changes; days computed at an older
# version are reprocessed rather than skipped.
#
# 2: added the price-coverage gate. The arithmetic is unchanged -- a day that
#    was fully priced computes bit-identically at 1 and 2 -- but version 1 could
#    not distinguish a fully-priced day from a partially-priced one, so a v1 row
#    carries no evidence that its prices were complete. Recomputing under v2
#    republishes every day that can be verified and simply omits those that
#    cannot, which orphans the unverifiable v1 rows instead of requiring them to
#    be hunted down and deleted.
# 3: added `load_kwh` (total house consumption, priced hours only) so the
#    dashboard can show a blended real €/kWh -- cost_model1 / load_kwh -- instead
#    of saving / avoided-import, which goes degenerate (near-zero or negative
#    denominator) whenever the battery's benefit comes mostly from price
#    arbitrage rather than pure import avoidance. cost1/cost2/saving/import*/
#    export* are otherwise unchanged from v2.
#    Keep the dashboard's `model_version` variable in step
#    (grafana/alphaess-battery-savings.json).
#
# `computed_at_unix` was added to the stored row after 3 without bumping to 4,
# which is a deliberate exception to the rule above rather than an oversight.
# It records when the job ran and takes no part in the arithmetic, so a v3 row
# written before it and one written after describe the same day identically.
# Bumping would have hidden every existing savings row behind an empty
# dashboard until a full backfill had run -- a real cost, for a field no panel
# reads. The staleness monitor tolerates its absence on older rows by design:
# max() simply ignores them.
MODEL_VERSION = "3"

# Defined before the settings below, which log through it while being parsed.
log = logging.getLogger("pricing")


def _num_env(name: str, default: str, cast=float):
    """Parse a numeric setting, failing loudly on a typo.

    These are read at import, so an unparseable value used to surface as a bare
    ValueError traceback from a module-level line -- the offending variable
    named nowhere in it. That was unreachable while nothing set these; now that
    docker-compose.yml passes them through it is a mistype away.

    Deliberately fatal rather than falling back to the default: a gate that
    silently reverts is how a day gets scored under rules nobody chose.
    """
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except ValueError:
        log.error("%s=%r is not a valid %s", name, raw, cast.__name__)
        sys.exit(1)


# Complete-day gate. Both are settable via .env -- see .env.example.
POLL_INTERVAL_S = _num_env("POLL_INTERVAL_SECONDS", "30", int)
MIN_COVERAGE = _num_env("PRICING_MIN_COVERAGE", "0.98")
MAX_GAP_S = _num_env("PRICING_MAX_GAP_S", "1200")  # 20 min

# Minimum fraction of the day that must be covered by price intervals. Unlike
# sample coverage this is held to ~1.0: energy in an unpriced hour is silently
# dropped by integrate_by_interval, so a day missing an hour of prices is not
# a slightly-noisier result, it is a wrong one (that hour's cost reads as zero
# in both models). The tolerance exists only to absorb float error on the
# boundary arithmetic, not to admit genuinely missing hours.
#
# Deliberately NOT configurable, unlike the two gates above -- this is not an
# oversight, so please don't "fix" it. Those two measure sample completeness and
# degrade smoothly, so trading precision for a day that would otherwise vanish
# is a real operational call. This one has no such gradient: below 1.0 the
# output is not noisier, it is wrong, and nothing in the stored row marks it.
# The fix for a day excluded here is to refetch its prices, never to lower the
# bar.
MIN_PRICE_COVERAGE = 0.999

# Optional: convert ΔSoC% to kWh for the borrow/bank indicator.
BATTERY_CAPACITY_KWH = os.environ.get("BATTERY_CAPACITY_KWH")


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
    so import and export are separated correctly even within one price slot.
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

        # The ramp parameters are bound as defaults rather than closed over:
        # interp is rebuilt per segment and only used inside this iteration, so
        # late binding is harmless today, but it is one refactor (collecting the
        # closures into a list, say) away from silently pricing every segment
        # with the last segment's slope.
        def interp(t, p0=p0, p1=p1, t0=t0, span=span):
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

def priced_seconds(intervals: list[dict], win_start: dt.datetime,
                   win_end: dt.datetime) -> float:
    """Seconds of [win_start, win_end) covered by at least one price interval.

    Overlapping intervals are merged rather than summed. A duplicate hour --
    which InfluxDB can hold when the same slot is written under two different
    tag sets -- would otherwise inflate the total past the length of the day
    and mask a genuine hole elsewhere.
    """
    spans = sorted(
        (max(iv["from"], win_start), min(iv["till"], win_end))
        for iv in intervals
        if iv["till"] > win_start and iv["from"] < win_end
    )
    covered = 0.0
    merged_start = merged_end = None
    for start, end in spans:
        if merged_end is None or start > merged_end:
            if merged_end is not None:
                covered += (merged_end - merged_start).total_seconds()
            merged_start, merged_end = start, end
        elif end > merged_end:
            merged_end = end
    if merged_end is not None:
        covered += (merged_end - merged_start).total_seconds()
    return covered


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
    house = integrate_by_interval(samples, lambda s: s.load, intervals)

    cost1 = cost2 = 0.0
    imp1 = exp1 = imp2 = exp2 = 0.0  # kWh totals
    load_kwh = 0.0
    for iv, (ia, ea), (ic, ec), (il, el) in zip(intervals, actual, counterfactual, house):
        pi, pe = import_price(iv), export_price(iv)
        ia, ea, ic, ec = ia / 1000, ea / 1000, ic / 1000, ec / 1000  # Wh -> kWh
        il, el = il / 1000, el / 1000
        cost1 += ia * pi - ea * pe
        cost2 += ic * pi - ec * pe
        imp1 += ia
        exp1 += ea
        imp2 += ic
        exp2 += ec
        # el is normally ~0 (load isn't expected to go negative); netting it
        # anyway matches how imp1/exp1 are combined above.
        load_kwh += il - el

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

    # Price coverage is tracked separately from sample coverage because the two
    # fail independently and only one of them is visible in the samples. Energy
    # in an hour with no price is dropped by integrate_by_interval, so a day
    # priced for 12 of its 24 hours produces a cost that is simply half of the
    # real one -- with sample coverage still reading 1.000. Without this metric
    # that day is indistinguishable from a correct one, and once written it is
    # skipped forever by _already_done.
    price_coverage = (priced_seconds(intervals, win_start, win_end) / day_len
                      if day_len else 0.0)

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
        "load_kwh": round(load_kwh, 4),
        "delta_soc_percent": round(samples[-1].soc - samples[0].soc, 2),
        "balance_residual_kwh": round(residual, 4),
        "coverage": round(coverage, 4),
        "price_coverage": round(price_coverage, 4),
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
    start = dt.datetime.combine(day, dt.time(), NL_TZ).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), NL_TZ).astimezone(dt.UTC)
    return start, end


def gate(result: dict) -> tuple[bool, str]:
    if result["coverage"] < MIN_COVERAGE:
        return False, f"coverage {result['coverage']:.3f} < {MIN_COVERAGE}"
    # Checked before max_gap because a partially-priced day is wrong rather
    # than merely thin, and the operator action differs: rerun once the
    # day-ahead prices land, instead of investigating a collector outage.
    if result["price_coverage"] < MIN_PRICE_COVERAGE:
        return False, (f"price coverage {result['price_coverage']:.3f} < "
                       f"{MIN_PRICE_COVERAGE} (prices missing for "
                       f"{(1 - result['price_coverage']) * 24:.1f}h of the day)")
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
    quality = (f"coverage={result['coverage']:.3f} "
               f"price_coverage={result['price_coverage']:.3f} "
               f"max_gap={result['max_gap_s']:.0f}s "
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
    # When the job ran, not the day it describes -- the staleness monitor reads
    # this. daily_cost rows are stamped at the local midnight of the day they
    # cover, so on a healthy system the newest row's own timestamp is already
    # ~51h old just before the next nightly run; a check against it would need
    # a threshold above that and would take two and a half days to notice a
    # dead job. Mirrors the field of the same name in efficiency.py.
    #
    # Set here rather than in compute_day's result: compute_day is re-run by
    # audit_day to judge rows already stored, and a wall-clock field would make
    # it return something different every call for identical inputs.
    point = point.field("computed_at_unix", float(int(time.time())))
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
    return any(table.records for table in query_api.query(flux))


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

_STORED_DAYS_FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{meas}" and r.sys_sn == "{sys_sn}"
        and r._field == "saving")
  |> keep(columns: ["_time", "model_version"])
  |> sort(columns: ["_time"])
"""


def stored_days(query_api, bucket, sys_sn, start, stop) -> list[tuple[dt.date, str]]:
    """Local days that already have a `daily_cost` row, with their model_version.

    The row is timestamped at the local midnight that opens the day, so
    converting back through NL_TZ recovers the day it describes -- taking
    .date() on the raw UTC instant would name the previous day for most of the
    year.
    """
    flux = _STORED_DAYS_FLUX.format(
        bucket=bucket, meas=DAILY_MEASUREMENT, sys_sn=sys_sn,
        start=start.isoformat(), stop=stop.isoformat(),
    )
    found = set()
    for table in query_api.query(flux):
        for rec in table.records:
            time = rec.get_time()
            if time is None:
                continue
            found.add((time.astimezone(NL_TZ).date(),
                       rec.values.get("model_version", "")))
    return sorted(found)


def audit_day(day, samples, intervals) -> tuple[str, str]:
    """Judge a stored row against what today's rules and data would produce.

    Returns (status, detail). "stale" means a row exists for a day that would
    now be rejected -- process_day returns early on a gate failure without
    touching what is already stored, so a rerun (even with --force) leaves the
    old figure in place. Those rows are the ones that need deleting by hand.
    """
    if not samples:
        return "stale", "no power samples for this day"
    if not intervals:
        return "stale", "no prices stored for this day"
    result = compute_day(samples, intervals, day)
    ok, why = gate(result)
    if ok:
        return "ok", (f"price_coverage={result['price_coverage']:.3f} "
                      f"saving=EUR{result['saving']:.4f}")
    return "stale", why


def run_audit(days: list[dt.date] | None) -> None:
    """Report stored `daily_cost` rows that today's gate would refuse to write.

    Answers the question a rerun cannot: --force recomputes and overwrites a
    day it accepts, but a day it now *rejects* keeps whatever was written under
    the old rules. This lists exactly those rows.
    """
    client = InfluxDBClient(url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"), org=env("INFLUX_ORG"))
    bucket = env("INFLUX_BUCKET")
    sys_sn = env("ALPHAESS_SYS_SN")
    query_api = client.query_api()
    try:
        if days:
            window = (day_window_utc(days[0])[0], day_window_utc(days[-1])[1])
        else:
            # Everything ever written. The measurement holds one row per day,
            # so a wide range is cheap.
            window = (dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
                      dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        rows = stored_days(query_api, bucket, sys_sn, *window)
        wanted = set(days) if days else None

        stale = []
        checked = 0
        superseded = 0
        for day, model_version in rows:
            if wanted is not None and day not in wanted:
                continue
            # Rows at an older model_version are orphaned by design -- the
            # dashboard reads one version at a time -- so they are counted and
            # left alone rather than reported as problems.
            if model_version != MODEL_VERSION:
                superseded += 1
                continue
            checked += 1
            start, end = day_window_utc(day)
            samples = load_samples_influx(query_api, bucket, sys_sn, start, end)
            intervals = load_prices_influx(query_api, bucket, start, end)
            status, detail = audit_day(day, samples, intervals)
            if status == "ok":
                log.info("%s: OK [%s]", day, detail)
            else:
                stale.append((day, detail))
                log.warning("%s: STALE -- %s", day, detail)

        log.info("Audited %d stored day(s) at model_version=%s: %d OK, %d stale",
                 checked, MODEL_VERSION, checked - len(stale), len(stale))
        if superseded:
            log.info(
                "%d row(s) remain at an earlier model_version. They are "
                "invisible to the dashboard while it is set to %s, and can be "
                "left in place or deleted at leisure.", superseded, MODEL_VERSION)
        if stale:
            log.warning(
                "%d stored row(s) would be rejected by today's gate, which "
                "means the data behind them changed after they were written. A "
                "rerun will not correct them -- process_day leaves an excluded "
                "day's existing row untouched. Delete them, then re-run "
                "pricing.py for those days:", len(stale))
            for day, _detail in stale:
                start, end = day_window_utc(day)
                # Scoped to this model_version so the predicate cannot also
                # take out rows kept from an earlier one for comparison.
                log.warning(
                    "  influx delete --bucket %s --start %s --stop %s "
                    "--predicate '_measurement=\"%s\" AND sys_sn=\"%s\" AND "
                    "model_version=\"%s\"'",
                    bucket, start.isoformat(), end.isoformat(),
                    DAILY_MEASUREMENT, sys_sn, MODEL_VERSION)
    finally:
        client.close()


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
    p.add_argument("--audit", action="store_true",
                   help="Report stored days that today's quality gate would "
                        "reject. Read-only. Defaults to every stored day; "
                        "narrow with --date/--backfill.")
    args = p.parse_args()

    if args.audit:
        # No --date/--backfill means "everything ever written", not "yesterday".
        run_audit(resolve_days(args) if (args.date or args.backfill) else None)
        return

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
