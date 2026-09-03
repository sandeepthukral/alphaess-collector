"""Where the energy actually goes: metered load vs derived load -> InfluxDB.

`power_readings.load_power_w` is not a measurement. It is an exact arithmetic
identity -- `load == pv + grid + battery` holds on every sample AlphaESS has
ever returned from `getLastPowerData`, to the watt -- because the API derives
house load as the residual rather than metering it. So the whole-house AC
energy balance closes by construction, and the inverter's conversion and
standby losses are structurally invisible in that dataset. No amount of
integrating it can show them.

`getOneDayPowerBySn` reports the same day at 5-minute resolution with a `load`
that is *not* that residual. The gap between the two is what the residual
cannot see: on 2026-08-05 the metered load integrated to 17.86 kWh against a
derived 19.25 kWh, i.e. 1.39 kWh unaccounted in a single day, while the two
sources' SoC and feed-in agreed to a fraction of a percent.

That gives a loss decomposition that holds over any window, with no
"start and end SoC must match" precondition:

    conversion + standby = sum(derived load - metered load)
    battery internal     = sum(charge) - sum(discharge) - dSoC * capacity
    total                = the two added

The two terms do not double-count: `getOneDateEnergyBySn`'s eCharge/eDischarge
reproduce this repo's own integration of `battery_power_w` to within 1.4% over
19 days, so they measure the same plane, and the conversion term is measured
entirely outside it. See DESIGN-battery-savings.md, "Where the losses come
from".

Run modes:
    python efficiency.py                                # yesterday (NL)
    python efficiency.py --date 2026-08-05
    python efficiency.py --backfill 2026-07-18 2026-08-05
    python efficiency.py --date 2026-08-05 --dry-run    # compute + print, no write
    python efficiency.py --date 2026-08-05 --check-alignment  # clock probe, read-only
    python efficiency.py --system-facts                 # getEssList, read-only
"""

import argparse
import bisect
import datetime as dt
import logging
import os
import statistics
import sys
import time

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from collector import API_BASE, auth_headers, send_heartbeat, write_health_event
from prices import NL_TZ
from pricing import (
    MIN_COVERAGE,
    POLL_INTERVAL_S,
    Sample,
    _num_env,
    day_window_utc,
    integrate_by_interval,
    load_samples_influx,
    resolve_days,
)

SERIES_MEASUREMENT = "metered_power"
DAILY_MEASUREMENT = "daily_energy"

# Provenance tag on the series. Two AlphaESS endpoints report overlapping
# quantities that do NOT agree (getOneDayPowerBySn integrates its feedIn to
# 12.28 kWh on a day getOneDateEnergyBySn calls 11.55), so which endpoint a
# number came from has to survive into the database.
SERIES_SOURCE = "getOneDayPowerBySn"

# Native cadence of getOneDayPowerBySn: 288 records/day. Used for coverage
# accounting only -- the real spacing is read from the records themselves.
SERIES_CADENCE_S = 300

# Bump when the stored schema or the loss definition changes; days computed at
# an older version are reprocessed rather than skipped. Keep the dashboard's
# `model_version` variable in step (grafana/alphaess-energy-losses.json).
MODEL_VERSION = "1"

# Defined before the settings below, which log through it while being parsed.
log = logging.getLogger("efficiency")

# Quality gate. See gate() for why they are checked in this order.
MIN_SERIES_COVERAGE = _num_env("EFFICIENCY_MIN_COVERAGE", "0.98")
MAX_SERIES_GAP_S = _num_env("EFFICIENCY_MAX_GAP_S", "1800")
MAX_SOC_ALIGN_PP = _num_env("EFFICIENCY_MAX_SOC_ALIGN_PP", "2.0")

# Despiking for the metered load series. AlphaESS intermittently returns a
# wildly wrong value in a single 5-minute `load` record -- 5832 W where both
# neighbours and the 30 s series agree the house was drawing ~500 W. Measured
# across 19 days: 26 records in 5430 (0.48%), worth 8.7 kWh of phantom load,
# with 14 of them landing on 2026-08-01 alone -- enough to take that day's
# conversion loss to -4.59 kWh, i.e. to invert the sign of the quantity this
# module exists to measure.
#
# The test is against the derived series rather than against neighbouring
# metered records, because it is the physically meaningful one: conversion loss
# makes derived >= metered, always and in that direction only. A record where
# metered exceeds derived several-fold is not measuring loss, whatever else it
# is. A genuine appliance spike appears in *both* series and is kept; a
# neighbour-based filter would have thrown it away.
SPIKE_FACTOR = _num_env("EFFICIENCY_SPIKE_FACTOR", "4.0")
SPIKE_FLOOR_W = _num_env("EFFICIENCY_SPIKE_FLOOR_W", "1500")
SPIKE_WINDOW_HALF_S = 150.0  # half the native cadence, either side of the record
MAX_SPIKE_FRACTION = _num_env("EFFICIENCY_MAX_SPIKE_FRACTION", "0.10")

# Rate-limit politeness. The upstream alphaess-openAPI library advises 10s
# between any AlphaCloud calls, and this process shares an appId rate budget
# with collector.py, which is polling every 30s from the same image. A backfill
# that hammers the API pushes live collection into its backoff ladder and
# punches gaps in power_readings, which pricing.py then charges against
# PRICING_MAX_GAP_S -- i.e. a greedy backfill can cost a day of daily_cost.
MIN_REQUEST_INTERVAL_S = _num_env("ALPHAESS_MIN_REQUEST_INTERVAL_S", "10")
MAX_RETRIES = _num_env("ALPHAESS_MAX_RETRIES", "4", int)

# 6053 is "the request was too fast, please try again later" -- the throttle
# actually hit in practice. Overridable because AlphaESS's code list is not
# published anywhere authoritative. Note 6006 (timestamp/clock skew) is
# deliberately NOT here: retrying a signature the server rejects as stale just
# burns the rate budget.
RETRY_CODES = {c.strip() for c in os.environ.get("ALPHAESS_RETRY_CODES", "6053").split(",")
               if c.strip()}

# Abort a multi-day run after this many consecutive days lost to throttling.
# A 60-day backfill that keeps hammering a throttled API is worse than one that
# stops and gets re-run tomorrow.
THROTTLE_CIRCUIT_BREAK = 3

# Optional: converts dSoC% into the kWh still sitting in the battery, without
# which the battery term cannot be separated from a charge/discharge imbalance.
BATTERY_CAPACITY_KWH = os.environ.get("BATTERY_CAPACITY_KWH")

# Optional Kuma "Push" monitor URL; unset = no heartbeat.
HEARTBEAT_URL = os.environ.get("EFFICIENCY_HEARTBEAT_URL", "")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


class ApiError(RuntimeError):
    """A response this run should not retry: bad credentials, unknown SN."""


class ThrottledError(RuntimeError):
    """Retries exhausted against a rate limit or transport failure."""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _get(path: str, params: dict, app_id: str, app_secret: str):
    """One AlphaESS GET with throttling and bounded retries.

    Returns the response's `data` member (which may legitimately be None --
    see _is_blank_energy). Raises ApiError for anything retrying cannot fix,
    ThrottledError once the retries are spent.
    """
    attempt = 0
    while True:
        _throttle()
        retryable = None
        try:
            resp = requests.get(
                f"{API_BASE}/{path}",
                params=params,
                headers=auth_headers(app_id, app_secret),
                timeout=30,
            )
        except requests.RequestException as exc:
            retryable = str(exc)
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                retryable = f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                raise ApiError(f"{path}: HTTP {resp.status_code}")
            else:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise ApiError(f"{path}: response is not JSON ({exc})") from exc
                code = str(body.get("code"))
                if code in RETRY_CODES:
                    retryable = f"code {code} ({body.get('msg')})"
                elif code != "200":
                    raise ApiError(f"{path}: code {code} ({body.get('msg')})")
                else:
                    return body.get("data")

        attempt += 1
        if attempt > MAX_RETRIES:
            raise ThrottledError(f"{path}: {retryable} after {MAX_RETRIES} retries")
        delay = 2 ** attempt
        log.warning("%s: %s -- retrying in %ds (%d/%d)",
                    path, retryable, delay, attempt, MAX_RETRIES)
        time.sleep(delay)


def fetch_day_power(app_id: str, app_secret: str, sys_sn: str, day: dt.date) -> list[dict]:
    """5-minute records for one past local day. The monkeypatch seam for tests."""
    data = _get("getOneDayPowerBySn", {"sysSn": sys_sn, "queryDate": day.isoformat()},
                app_id, app_secret)
    return data if isinstance(data, list) else []


def fetch_day_energy(app_id: str, app_secret: str, sys_sn: str, day: dt.date) -> dict:
    """Daily kWh totals for one past local day. The monkeypatch seam for tests."""
    data = _get("getOneDateEnergyBySn", {"sysSn": sys_sn, "queryDate": day.isoformat()},
                app_id, app_secret)
    return data if isinstance(data, dict) else {}


def fetch_system_facts(app_id: str, app_secret: str) -> list[dict]:
    data = _get("getEssList", {}, app_id, app_secret)
    return data if isinstance(data, list) else []


ENERGY_KEYS = ("eCharge", "eDischarge", "epv", "eOutput", "eInput", "eGridCharge")


def _is_blank_energy(data: dict | None) -> bool:
    """True for the all-zero / null payload AlphaESS returns around local
    midnight and during its overnight quiet period.

    It arrives as HTTP 200 with code 200, so naive unmarshalling turns it into
    a legitimate-looking day of zero energy -- which then gets written, marked
    done, and never revisited.
    """
    if not data:
        return True
    return all(_f(data.get(k)) == 0.0 for k in ENERGY_KEYS)


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# uploadTime -> UTC
# --------------------------------------------------------------------------

def _is_ambiguous(local: dt.datetime) -> bool:
    """True inside the repeated hour of an autumn DST transition."""
    return local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()


def _is_nonexistent(local: dt.datetime, naive: dt.datetime) -> bool:
    """True inside the skipped hour of a spring DST transition.

    A local time that does not exist still converts to *some* instant, and
    converting that instant back lands on a different wall clock. Round-tripping
    is the only way to notice.
    """
    return local.astimezone(dt.UTC).astimezone(NL_TZ).replace(tzinfo=None) != naive


def parse_upload_times(raw: list[dict], day: dt.date) -> list[tuple[dt.datetime, dict]]:
    """Convert each record's naive-local `uploadTime` to an aware UTC instant.

    Returns (instant, record) pairs sorted by instant, clipped to the local day
    and deduplicated (last wins -- AlphaESS has been observed returning
    duplicate rows).

    DST is handled explicitly rather than left to ZoneInfo's defaults:

    * Autumn: 02:00-02:59 local happens twice and `fold` defaults to 0 for
      both, so the second pass would silently overwrite the first -- a 25-hour
      day stored as 24 with an hour of energy missing. The records arrive in
      chronological order, so the first backwards step inside an ambiguous hour
      marks where the fold began.
    * Spring: 02:00-02:59 local does not exist. Those records are dropped, not
      coerced onto a neighbouring instant.
    """
    win_start, win_end = day_window_utc(day)
    ordered: list[tuple[dt.datetime, dict]] = []
    prev: dt.datetime | None = None
    folded = False
    dropped_gap = 0

    for rec in raw:
        value = rec.get("uploadTime")
        if not value:
            continue
        try:
            naive = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            log.warning("%s: unparseable uploadTime %r, dropping", day, value)
            continue

        local = naive.replace(tzinfo=NL_TZ)
        if _is_nonexistent(local, naive):
            dropped_gap += 1
            continue

        instant = local.astimezone(dt.UTC)
        if _is_ambiguous(local):
            if folded or (prev is not None and instant <= prev):
                folded = True
                instant = local.replace(fold=1).astimezone(dt.UTC)
        else:
            folded = False

        if not (win_start <= instant < win_end):
            continue
        ordered.append((instant, rec))
        prev = instant

    if dropped_gap:
        log.warning("%s: dropped %d record(s) inside the spring DST gap",
                    day, dropped_gap)

    deduped: dict[dt.datetime, dict] = {}
    for instant, rec in ordered:
        deduped[instant] = rec
    return sorted(deduped.items())


def metered_samples(parsed: list[tuple[dt.datetime, dict]]) -> list[Sample]:
    """Wrap the metered series in pricing.Sample so the *same* integrator runs
    over both load figures. Two integration conventions is how two answers that
    quietly disagree get created."""
    return [
        Sample(time=instant, pv=0.0, grid=0.0, load=_f(rec.get("load")),
               battery=0.0, soc=_f(rec.get("cbat")))
        for instant, rec in parsed
    ]


# --------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------

def net_kwh(samples: list[Sample], power_fn, win_start, win_end) -> float:
    """Integrate a power signal over one window into signed kWh."""
    if len(samples) < 2:
        return 0.0
    (imported, exported), = integrate_by_interval(
        samples, power_fn, [{"from": win_start, "till": win_end}])
    return (imported - exported) / 1000.0


def interpolate_at(samples: list[Sample], instants: list[dt.datetime], value_fn):
    """Linearly interpolate value_fn over `samples` at each instant.

    Instants outside the sampled span yield None rather than an extrapolation:
    the head and tail of a day are exactly where a collector outage lives, and
    flat-extending across one would forge the number this module exists to
    measure.
    """
    if not samples:
        return [None] * len(instants)
    times = [s.time for s in samples]
    out: list[float | None] = []
    for t in instants:
        i = bisect.bisect_left(times, t)
        if i == 0:
            out.append(value_fn(samples[0]) if times[0] == t else None)
            continue
        if i >= len(times):
            out.append(None)
            continue
        a, b = samples[i - 1], samples[i]
        span = (b.time - a.time).total_seconds()
        f = 0.0 if span <= 0 else (t - a.time).total_seconds() / span
        out.append(value_fn(a) + (value_fn(b) - value_fn(a)) * f)
    return out


def drop_implausible(metered: list[Sample],
                     readings: list[Sample]) -> tuple[list[Sample], list[Sample]]:
    """Split the metered series into (kept, dropped) on the derived cross-check.

    Returns records whose load is several times the largest derived load
    anywhere in the surrounding 5 minutes as `dropped`. See SPIKE_FACTOR for
    why that is the right test and why a neighbour-based one is not.

    The window `max` rather than a point interpolation is deliberate: the 30 s
    series has its own transient glitches (momentarily inconsistent
    pv/grid/battery snapshots produce load values as low as -7.8 kW), and one
    of those landing on a metered instant would otherwise condemn a perfectly
    good record. Taking the maximum over the record's own 5-minute
    neighbourhood makes the test hard to trip by accident, which is the right
    bias for a filter that removes data.

    Records with no derived samples nearby are always kept -- there is nothing
    to judge them against, and a filter that discards what it cannot check
    would eat exactly the collector outages the coverage gate is there to
    catch.
    """
    if SPIKE_FACTOR <= 0 or not readings:
        return metered, []
    times = [s.time for s in readings]
    half = dt.timedelta(seconds=SPIKE_WINDOW_HALF_S)
    kept, dropped = [], []
    for sample in metered:
        lo = bisect.bisect_left(times, sample.time - half)
        hi = bisect.bisect_right(times, sample.time + half)
        if lo >= hi:
            kept.append(sample)
            continue
        ceiling = max(0.0, max(readings[i].load for i in range(lo, hi)))
        if sample.load > SPIKE_FACTOR * ceiling and sample.load - ceiling > SPIKE_FLOOR_W:
            dropped.append(sample)
        else:
            kept.append(sample)
    return kept, dropped


def coverage_of(samples: list[Sample], win_start, win_end,
                cadence_s: float) -> tuple[float, float]:
    """(time-based coverage, largest gap in seconds) for a sampled series.

    Same accounting as pricing.compute_day: cadence drift and the odd skipped
    sample never count as missing, only real gaps (beyond 3x cadence) plus any
    unsampled head and tail of the day.
    """
    day_len = (win_end - win_start).total_seconds()
    if not samples or day_len <= 0:
        return 0.0, 0.0
    gaps = [(b.time - a.time).total_seconds() for a, b in zip(samples, samples[1:])]
    max_gap = max(gaps) if gaps else 0.0
    head = max(0.0, (samples[0].time - win_start).total_seconds())
    tail = max(0.0, (win_end - samples[-1].time).total_seconds())
    outage = sum(g - cadence_s for g in gaps if g > 3 * cadence_s)
    return max(0.0, 1.0 - (head + tail + outage) / day_len), max_gap


def soc_alignment(metered: list[Sample], readings: list[Sample]) -> tuple[float, float]:
    """(median, max) absolute SoC disagreement in percentage points.

    The two sources report the same physical quantity by different paths, so
    this is an independent check on the uploadTime timezone assumption -- which
    is assumed, not documented. A one-hour error moves SoC by tens of points on
    any day the battery is cycling, so a DST mis-parse fails loudly here instead
    of arriving as a plausible-looking loss figure.
    """
    interp = interpolate_at(readings, [s.time for s in metered], lambda s: s.soc)
    diffs = [abs(m.soc - v) for m, v in zip(metered, interp) if v is not None]
    if not diffs:
        return 0.0, 0.0
    return statistics.median(diffs), max(diffs)


def compute_day(day: dt.date, parsed: list[tuple[dt.datetime, dict]],
                energy: dict, readings: list[Sample]) -> dict:
    """Build the daily_energy fields from both endpoints plus power_readings."""
    win_start, win_end = day_window_utc(day)
    raw_metered = metered_samples(parsed)
    metered, dropped = drop_implausible(raw_metered, readings)

    metered_load = net_kwh(metered, lambda s: s.load, win_start, win_end)
    derived_load = net_kwh(readings, lambda s: s.load, win_start, win_end)

    # The same derived series resampled onto the metered instants. If the two
    # load integrals differed merely because one is sampled 10x finer, this
    # would move to meet the metered figure; it does not, which is what makes
    # conversion_loss_kwh a physical quantity rather than a sampling artefact.
    instants = [s.time for s in metered]
    resampled = [
        Sample(time=t, pv=0.0, grid=0.0, load=v, battery=0.0, soc=0.0)
        for t, v in zip(instants, interpolate_at(readings, instants, lambda s: s.load))
        if v is not None
    ]
    derived_at_5m = net_kwh(resampled, lambda s: s.load, win_start, win_end)

    charge = _f(energy.get("eCharge"))
    discharge = _f(energy.get("eDischarge"))
    # Coverage is measured on the series as delivered, not on what survived the
    # despike: the two answer different questions, and conflating them would let
    # a day of mostly-corrupt records read as fully covered. The drop count is
    # stored and gated separately.
    series_cov, series_gap = coverage_of(raw_metered, win_start, win_end, SERIES_CADENCE_S)
    readings_cov, readings_gap = coverage_of(readings, win_start, win_end, POLL_INTERVAL_S)
    # On the raw series: the despike targets the `load` field, and dropping a
    # record's SoC alongside it would shrink the sample the clock check runs on
    # for no reason.
    align_median, align_max = soc_alignment(raw_metered, readings)

    result = {
        # Reported by getOneDateEnergyBySn, verbatim. The _api suffix is not
        # decoration: daily_cost.export_kwh_actual, daily_energy.export_kwh_api
        # and the raw feed_in_w series are three different numbers from three
        # different provenances, and must never meet in one expression.
        "charge_kwh_api": charge,
        "discharge_kwh_api": discharge,
        "pv_kwh_api": _f(energy.get("epv")),
        "export_kwh_api": _f(energy.get("eOutput")),
        "import_kwh_api": _f(energy.get("eInput")),
        "grid_charge_kwh_api": _f(energy.get("eGridCharge")),

        "metered_load_kwh": round(metered_load, 4),
        "derived_load_kwh": round(derived_load, 4),
        "derived_load_kwh_at_5m": round(derived_at_5m, 4),
        "conversion_loss_kwh": round(derived_load - metered_load, 4),

        # Deliberately not called a loss: it is only one over a window whose
        # start and end SoC match. battery_loss_kwh below is the corrected form.
        "charge_minus_discharge_kwh": round(charge - discharge, 4),
        "delta_soc_percent": round(readings[-1].soc - readings[0].soc, 2) if readings else 0.0,

        "series_count": len(raw_metered),
        "series_dropped": len(dropped),
        "series_dropped_kwh": round(
            sum(s.load for s in dropped) * SERIES_CADENCE_S / 3600.0 / 1000.0, 4),
        "series_coverage": round(series_cov, 4),
        "series_max_gap_s": round(series_gap, 1),
        "readings_coverage": round(readings_cov, 4),
        "readings_max_gap_s": round(readings_gap, 1),
        "soc_align_median_pp": round(align_median, 3),
        "soc_align_max_pp": round(align_max, 3),

        # When the job ran, not the day it describes. Every staleness check
        # reads this: daily_energy rows are stamped at the local midnight of
        # the day analysed, so on a healthy system the newest row's own
        # timestamp is already 51h old right before the next nightly run.
        "computed_at_unix": float(int(time.time())),
    }

    if BATTERY_CAPACITY_KWH:
        stored = result["delta_soc_percent"] / 100 * float(BATTERY_CAPACITY_KWH)
        result["delta_soc_kwh"] = round(stored, 4)
        result["battery_loss_kwh"] = round(charge - discharge - stored, 4)
        result["total_loss_kwh"] = round(
            result["conversion_loss_kwh"] + result["battery_loss_kwh"], 4)
    return result


def gate(result: dict) -> tuple[bool, str]:
    """Whether this day's derived figures are trustworthy enough to store."""
    # Checked first because a thin metered series under-states measured load and
    # therefore *over*-states the loss -- it fails in the flattering direction,
    # which is the one nobody investigates.
    if result["series_coverage"] < MIN_SERIES_COVERAGE:
        return False, (f"series coverage {result['series_coverage']:.3f} < "
                       f"{MIN_SERIES_COVERAGE}")
    # Before the gap check: misaligned clocks make the day wrong rather than
    # merely thin, and the operator action differs (run --check-alignment,
    # rather than investigate a collector outage).
    if result["soc_align_median_pp"] > MAX_SOC_ALIGN_PP:
        return False, (f"soc_align {result['soc_align_median_pp']:.1f}pp > "
                       f"{MAX_SOC_ALIGN_PP}")
    # A handful of corrupt records is normal and handled; a large fraction of
    # them means the payload as a whole is not to be trusted, and quietly
    # deleting a tenth of a day is not a repair.
    if result["series_count"]:
        spike_fraction = result["series_dropped"] / result["series_count"]
        if spike_fraction > MAX_SPIKE_FRACTION:
            return False, (f"{result['series_dropped']}/{result['series_count']} metered "
                           f"records implausible ({spike_fraction:.1%} > "
                           f"{MAX_SPIKE_FRACTION:.0%})")
    if result["readings_coverage"] < MIN_COVERAGE:
        return False, (f"readings coverage {result['readings_coverage']:.3f} < "
                       f"{MIN_COVERAGE}")
    if result["series_max_gap_s"] > MAX_SERIES_GAP_S:
        return False, (f"series max gap {result['series_max_gap_s']:.0f}s > "
                       f"{MAX_SERIES_GAP_S:.0f}s")
    return True, "ok"


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def series_points(parsed: list[tuple[dt.datetime, dict]], sys_sn: str) -> list[Point]:
    """Raw 5-minute records as InfluxDB points.

    `ppv` is NOT stored. It reads 0.0 on every record on this system -- three
    separate full days summed to 0.00 kWh while getOneDateEnergyBySn reported
    17-25 kWh of PV for the same days -- so storing it would render as a night
    that never ends and would silently corrupt any panel that trusted it. PV
    comes from pv_kwh_api and from power_readings.pv_power_w. Please do not
    "fix" the missing column by adding it back. `pchargingPile` is omitted for
    the simpler reason that there is no EV charger.
    """
    points = []
    for instant, rec in parsed:
        points.append(
            Point(SERIES_MEASUREMENT)
            .tag("sys_sn", sys_sn)
            .tag("source", SERIES_SOURCE)
            .field("metered_load_w", _f(rec.get("load")))
            .field("metered_soc_percent", _f(rec.get("cbat")))
            .field("feed_in_w", _f(rec.get("feedIn")))
            .field("grid_charge_w", _f(rec.get("gridCharge")))
            .time(instant, WritePrecision.S)
        )
    return points


def daily_point(day: dt.date, result: dict, sys_sn: str) -> Point:
    point = (
        Point(DAILY_MEASUREMENT)
        .tag("sys_sn", sys_sn)
        .tag("model_version", MODEL_VERSION)
        .time(day_window_utc(day)[0], WritePrecision.S)
    )
    for key, value in result.items():
        if value is not None:
            point = point.field(key, float(value))
    return point


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
# Orchestration
# --------------------------------------------------------------------------

class RunSummary:
    """What the run did, in the shape the heartbeat message needs.

    The interesting case is `gated`: a day that fetched cleanly and computed
    fine but failed the quality gate. That run exits 0 and writes nothing, so
    every check short of this one reads it as a healthy night.
    """

    def __init__(self):
        self.written: list[dt.date] = []
        self.skipped: list[dt.date] = []       # already done at this model_version
        self.gated: list[tuple[dt.date, str]] = []
        self.throttled: list[dt.date] = []
        self.failed: list[tuple[dt.date, str]] = []
        self.empty: list[dt.date] = []
        self.last_result: dict | None = None
        # Carried out of run_influx so main() can record a failed heartbeat push
        # against the same bucket/sys_sn the run itself wrote to, without
        # threading a write_api through the argparse plumbing.
        self.write_api = None
        self.bucket: str = ""
        self.sys_sn: str = ""

    @property
    def attempted(self) -> int:
        return len(self.written) + len(self.gated) + len(self.throttled) \
            + len(self.failed) + len(self.empty)

    def heartbeat(self) -> tuple[str, str] | None:
        """(status, msg) to push, or None when nothing should be pushed."""
        if self.written:
            day = self.written[-1]
            res = self.last_result or {}
            total = res.get("total_loss_kwh")
            detail = (f"total loss {total:.2f} kWh "
                      f"(conv {res.get('conversion_loss_kwh', 0):.2f} + "
                      f"bat {res.get('battery_loss_kwh', 0):.2f})"
                      if total is not None else
                      f"conversion loss {res.get('conversion_loss_kwh', 0):.2f} kWh")
            return "up", (f"OK {day}: {detail}; {len(self.written)} written, "
                          f"{len(self.skipped)} skipped")
        if self.throttled:
            return "down", (f"THROTTLED: {len(self.throttled)} day(s) lost to rate "
                            f"limiting, newest {self.throttled[-1]}")
        if self.failed:
            day, why = self.failed[-1]
            return "down", f"FAILED {day}: {why} ({len(self.failed)} day(s))"
        if self.gated:
            day, why = self.gated[-1]
            return "down", f"GATED {day}: {why} ({len(self.gated)} day(s))"
        if self.empty:
            return "down", (f"NO DATA: AlphaESS returned nothing for "
                            f"{len(self.empty)} day(s), newest {self.empty[-1]}")
        if self.skipped:
            # A night with nothing new to do is healthy, not silence. Without
            # this the monitor would flap every time the window is already full.
            return "up", f"OK (nothing new, up to date through {self.skipped[-1]})"
        return None


def process_day(day, parsed, energy, readings, dry_run, write_ctx,
                summary: RunSummary) -> None:
    result = compute_day(day, parsed, energy, readings)
    ok, why = gate(result)
    quality = (f"series_cov={result['series_coverage']:.3f} "
               f"readings_cov={result['readings_coverage']:.3f} "
               f"soc_align={result['soc_align_median_pp']:.2f}pp "
               f"max_gap={result['series_max_gap_s']:.0f}s "
               f"dropped={result['series_dropped']}/{result['series_count']}")
    if not ok:
        log.warning("%s: EXCLUDED (%s) [%s]", day, why, quality)
        summary.gated.append((day, why))
        return

    log.info(
        "%s: metered %.2f kWh vs derived %.2f kWh -> conversion loss %.2f kWh; "
        "battery in/out %.1f/%.1f kWh (dSoC %+.1f%%) [%s]",
        day, result["metered_load_kwh"], result["derived_load_kwh"],
        result["conversion_loss_kwh"], result["charge_kwh_api"],
        result["discharge_kwh_api"], result["delta_soc_percent"], quality,
    )
    if "total_loss_kwh" in result:
        log.info("%s: battery loss %.2f kWh, TOTAL %.2f kWh",
                 day, result["battery_loss_kwh"], result["total_loss_kwh"])

    if dry_run:
        return
    write_api, bucket, sys_sn = write_ctx
    write_api.write(bucket=bucket, record=daily_point(day, result, sys_sn))
    summary.written.append(day)
    summary.last_result = result
    log.info("%s: wrote %s", day, DAILY_MEASUREMENT)


def run_influx(days: list[dt.date], dry_run: bool, force: bool) -> RunSummary:
    app_id = env("ALPHAESS_APP_ID")
    app_secret = env("ALPHAESS_APP_SECRET")
    sys_sn = env("ALPHAESS_SYS_SN")
    client = InfluxDBClient(url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"),
                            org=env("INFLUX_ORG"))
    bucket = env("INFLUX_BUCKET")
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    summary = RunSummary()
    summary.write_api, summary.bucket, summary.sys_sn = write_api, bucket, sys_sn
    consecutive_throttles = 0
    try:
        for day in days:
            if not force and not dry_run and _already_done(query_api, bucket, sys_sn, day):
                log.info("%s: already processed at model_version=%s, skipping",
                         day, MODEL_VERSION)
                summary.skipped.append(day)
                continue
            try:
                raw = fetch_day_power(app_id, app_secret, sys_sn, day)
                energy = fetch_day_energy(app_id, app_secret, sys_sn, day)
            except ThrottledError as exc:
                log.error("%s: %s", day, exc)
                summary.throttled.append(day)
                consecutive_throttles += 1
                if consecutive_throttles >= THROTTLE_CIRCUIT_BREAK:
                    log.error("Aborting: %d consecutive days lost to throttling",
                              consecutive_throttles)
                    break
                continue
            except ApiError as exc:
                log.error("%s: %s", day, exc)
                summary.failed.append((day, str(exc)))
                continue
            consecutive_throttles = 0

            if not raw or _is_blank_energy(energy):
                # Never treat this as a day of zeros: see _is_blank_energy.
                log.warning("%s: AlphaESS returned no usable data, skipping", day)
                summary.empty.append(day)
                continue

            parsed = parse_upload_times(raw, day)
            if len(parsed) < 2:
                log.warning("%s: only %d usable metered record(s), skipping",
                            day, len(parsed))
                summary.empty.append(day)
                continue

            start, end = day_window_utc(day)
            readings = load_samples_influx(query_api, bucket, sys_sn, start, end)
            if len(readings) < 2:
                log.warning("%s: no power_readings, skipping", day)
                summary.empty.append(day)
                continue

            if not dry_run:
                # Written regardless of the gate below. Raw upstream data is
                # worth keeping on a day whose derived figures are not, and it
                # is what makes a recompute possible without re-hitting a
                # rate-limited API.
                write_api.write(bucket=bucket, record=series_points(parsed, sys_sn))
                log.info("%s: wrote %d %s points", day, len(parsed), SERIES_MEASUREMENT)

            process_day(day, parsed, energy, readings, dry_run,
                        (write_api, bucket, sys_sn), summary)
    finally:
        client.close()
    return summary


def run_check_alignment(days: list[dt.date]) -> None:
    """Report the clock lag that best reconciles the two SoC series. Read-only.

    The diagnostic to reach for when a day is gated on soc_align: it says
    whether uploadTime is being read in the wrong timezone (a whole-hour
    answer) or whether the disagreement is something else.
    """
    app_id = env("ALPHAESS_APP_ID")
    app_secret = env("ALPHAESS_APP_SECRET")
    sys_sn = env("ALPHAESS_SYS_SN")
    client = InfluxDBClient(url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"),
                            org=env("INFLUX_ORG"))
    bucket = env("INFLUX_BUCKET")
    query_api = client.query_api()
    try:
        for day in days:
            raw = fetch_day_power(app_id, app_secret, sys_sn, day)
            parsed = parse_upload_times(raw, day)
            if not parsed:
                log.warning("%s: no metered records", day)
                continue
            start, end = day_window_utc(day)
            # Widen by the search range so a shifted series still has readings
            # to compare against at both ends.
            readings = load_samples_influx(
                query_api, bucket, sys_sn,
                start - dt.timedelta(hours=4), end + dt.timedelta(hours=4))
            metered = metered_samples(parsed)
            best = None
            for step in range(-36, 37):  # +/- 3h in 5-minute steps
                offset = dt.timedelta(minutes=5 * step)
                shifted = [Sample(time=s.time + offset, pv=0.0, grid=0.0,
                                  load=s.load, battery=0.0, soc=s.soc)
                           for s in metered]
                median, _ = soc_alignment(shifted, readings)
                if best is None or median < best[1]:
                    best = (offset, median)
            offset, median = best
            log.info("%s: best lag %+.2fh, median SoC disagreement %.2fpp "
                     "(0.00h expected; a whole-hour answer means the uploadTime "
                     "timezone assumption is wrong)",
                     day, offset.total_seconds() / 3600.0, median)
    finally:
        client.close()


def run_system_facts() -> None:
    """Print getEssList and check the hand-set capacity against it.

    battery_loss_kwh scales linearly with BATTERY_CAPACITY_KWH, which is a
    number typed into .env by hand. This is the one command that verifies it.
    """
    facts = fetch_system_facts(env("ALPHAESS_APP_ID"), env("ALPHAESS_APP_SECRET"))
    if not facts:
        log.error("getEssList returned nothing")
        return
    for system in facts:
        log.info("%s: inverter %s (%.1f kW), battery %s, capacity %.3f kWh "
                 "(usable %.0f%%), EMS %s",
                 system.get("sysSn"), system.get("minv"), _f(system.get("poinv")),
                 system.get("mbat"), _f(system.get("cobat")),
                 _f(system.get("usCapacity")), system.get("emsStatus"))
        cobat = _f(system.get("cobat"))
        if BATTERY_CAPACITY_KWH and cobat:
            configured = float(BATTERY_CAPACITY_KWH)
            drift = abs(configured - cobat) / cobat
            if drift > 0.01:
                log.warning("BATTERY_CAPACITY_KWH=%s disagrees with the reported "
                            "%.3f kWh by %.1f%% -- battery_loss_kwh is directly "
                            "proportional to it", configured, cobat, drift * 100)
            else:
                log.info("BATTERY_CAPACITY_KWH=%s matches (%.1f%% drift)",
                         configured, drift * 100)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Back-fill AlphaESS metered load and daily energy totals into InfluxDB.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", metavar="YYYY-MM-DD")
    g.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Reprocess even if already done.")
    p.add_argument("--check-alignment", action="store_true",
                   help="Report the clock lag that best reconciles the two SoC "
                        "series. Read-only, writes nothing.")
    p.add_argument("--system-facts", action="store_true",
                   help="Print getEssList and verify BATTERY_CAPACITY_KWH against "
                        "it. Read-only.")
    args = p.parse_args()

    if args.system_facts:
        run_system_facts()
        return
    if args.check_alignment:
        run_check_alignment(resolve_days(args))
        return

    summary = run_influx(resolve_days(args), dry_run=args.dry_run, force=args.force)

    # No heartbeat on a dry run: it computes nothing durable, and pushing "up"
    # for it would let a hand-run mask a broken nightly job.
    if args.dry_run:
        return
    beat = summary.heartbeat()
    if beat and HEARTBEAT_URL:
        # Monitoring is the least important thing this job does. send_heartbeat
        # already swallows its own transport errors; this makes sure nothing
        # else about the push -- a malformed URL, a mangled message -- can
        # discard a run whose rows are already safely written.
        try:
            ping_failed = send_heartbeat(HEARTBEAT_URL, status=beat[0], msg=beat[1])
        except Exception as exc:
            log.warning("Heartbeat push failed: %s", exc)
            return
        if ping_failed and summary.write_api is not None:
            write_health_event(
                summary.write_api, summary.bucket, summary.sys_sn, "heartbeat_failed",
                {"error": ping_failed}, stage="heartbeat",
                component="efficiency", monitor="efficiency")


if __name__ == "__main__":
    main()
