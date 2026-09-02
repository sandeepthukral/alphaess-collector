"""InfluxDB -> mijnbatterij.nl submitter.

Publishes this installation's live battery figures to the public benchmarking
platform at https://mijnbatterij.nl, under the "Doe-het-zelf" (DIY) control
provider -- the battery here is driven by this repo's own Modbus dispatcher,
not by a supported aansturingsleverancier.

Like awtrix-pusher, this service NEVER calls the AlphaESS API. Everything it
sends is already in InfluxDB:

    batteryCharge, batteryPower   power_readings, newest sample
    chargedToday, dischargedToday power_readings, battery_power_w integrated
                                  from the local (NL) midnight to now
    batteryResult                 pricing.compute_day() over today so far --
                                  the same two-world model daily_cost stores,
                                  run on a partial day
    batteryResultTotal            sum of stored daily_cost.saving + today
    totalBatteryCycles            sum of daily_energy.discharge_kwh_api over
                                  full-throughput equivalents, + an offset for
                                  the cycles that predate this collector

WHY compute_day RATHER THAN A SEPARATE INTRADAY MODEL: today's euro figure and
the day it becomes tomorrow have to agree, or the platform's daily total steps
at midnight by the difference between two models. The gate is deliberately NOT
applied -- gate() judges whether a day is trustworthy enough to STORE
permanently, and by construction a partial day fails its coverage check. A live
figure that self-corrects on the next submission is a different bargain from a
stored one that never will.

THE API IS SPECIFIED, at https://onbalansmarkt.com/help/api-docs/ (spec at
/help/api-docs/public-schema). This integration was first built from a
third-party integration's example instead, because a 400 was what eventually
named the docs -- so four things here were wrong and the live endpoint never
said so: every numeric field is a STRING, `loadBalancingActive` is an
'on'/'off' enum rather than a boolean, there is no `batteryPower` field at all
(it was sent and ignored), and per-day history belongs to /api/results/daily
rather than a `days` map on the monthly payload. Read the spec before adding a
field; do not extend the guesswork.

`mode` is NOT SENT by default. The spec says it overrides the mode configured
on the profile page, and that page already carries Modus
("Handmatig/doe-het-zelf") beside Aansturing ("Frank Energie"). Which subset of
the enum an account may use varies by provider: asserting "self_consumption"
here earned a 400 naming frank-energie while the profile was correct all
along.

Run modes:
    python mijnbatterij.py           # submit loop (production)
    python mijnbatterij.py --once    # build one payload, print it, submit
    python mijnbatterij.py --once --dry-run   # ... and do not submit
    python mijnbatterij.py --monthly 2026-08  # backfill a finished month
    python mijnbatterij.py --monthly 2026-07 2026-08 --dry-run
"""

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import efficiency
import pricing
from prices import NL_TZ
from pricing import Sample, _accumulate

log = logging.getLogger("mijnbatterij")

API_BASE = "https://api.mijnbatterij.nl"
LIVE_PATH = "/api/live"
ME_PATH = "/api/me"
MONTHLY_PATH = "/api/results/monthly"
DAILY_PATH = "/api/results/daily"

# From the published OpenAPI spec (https://onbalansmarkt.com/help/api-docs/,
# spec at /help/api-docs/public-schema). Which subset a given account may use
# depends on its Aansturing: frank-energie rejected `self_consumption` outright.
# `manual` is the one that matches a profile whose Modus reads
# "Handmatig/doe-het-zelf", which is what this installation is.
MODES = ("imbalance", "imbalance_aggressive", "manual", "day_ahead",
         "self_consumption", "self_consumption_plus")

STATUS_MEASUREMENT = "mijnbatterij_submit"
RANK_MEASUREMENT = "mijnbatterij_rank"

# The daily_cost model whose savings are summed into batteryResultTotal. Pinned
# to whatever pricing.py currently writes, so a model bump moves both together
# rather than silently mixing two models' euros in one total.
MODEL_VERSION = pricing.MODEL_VERSION
# The same, for daily_energy and totalBatteryCycles. Both jobs supersede a day
# by writing a new row at a new version and leaving the old one in place -- so a
# sum that does not filter on version counts every recomputed day twice. On
# daily_energy that would roughly double the cycle count on a public
# leaderboard, and it stays invisible until the first MODEL_VERSION bump.
ENERGY_MODEL_VERSION = efficiency.MODEL_VERSION

DEFAULT_INTERVAL_S = 300
DEFAULT_TIMEOUT_S = 15
# How long a cached all-time total is reused. The stored half only changes once
# a night, so re-summing every submission would be five queries an hour for one
# new row a day; today's half is recomputed every submission regardless.
DEFAULT_TOTALS_TTL_S = 3600
# Beyond this the newest sample is not "live" and nothing is submitted. The
# platform ranks installations against each other; a stale row published as
# current is worse than a missing one.
DEFAULT_STALE_AFTER_S = 600
# Longest sample-to-sample gap that battery_energy_kwh will integrate across.
# 3x the poll interval is the same line pricing.compute_day() draws between
# cadence drift and a real outage, so the two agree on what counts as missing.
DEFAULT_MAX_SAMPLE_GAP_S = 3 * pricing.POLL_INTERVAL_S
# Ceiling on how many missing daily_energy days one totals refresh will
# integrate out of power_readings. Each costs a day of samples; on a healthy
# system the list is empty or one long, and a list longer than this is a broken
# nightly job rather than something to paper over one query at a time.
DEFAULT_MAX_FILL_DAYS = 10


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def _num_env(name: str, default: float, cast=float):
    """A numeric setting, treating an EMPTY value as absent.

    docker-compose.yml passes several of these through as `${VAR:-}`, so an
    unset variable arrives here as "" rather than not at all -- and float("")
    raises, crash-looping the container at startup over a setting nobody
    touched. os.environ.get's default only covers the absent case.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _str_env(name: str, default: str) -> str:
    """A string setting, treating an EMPTY value as absent.

    Same reasoning as _num_env, different symptom. `MIJNBATTERIJ_BASE_URL=` in
    .env leaves base_url as "", every POST goes to "/api/live" with no scheme,
    and requests raises MissingSchema -- a RequestException, so submit() wraps
    it into SubmitError and the loop retries it politely forever without ever
    naming the setting that is wrong.
    """
    return os.environ.get(name, "").strip() or default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Windows and integration
# --------------------------------------------------------------------------

def today_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) in UTC, from the current local (NL) midnight to `now`.

    The local midnight, not a rolling 24 hours: "chargedToday" on a leaderboard
    is a calendar day in the installation's own timezone, and this is the same
    boundary daily_cost and daily_energy already use.
    """
    now = now.astimezone(dt.UTC)
    local_date = now.astimezone(NL_TZ).date()
    start = dt.datetime.combine(local_date, dt.time(), NL_TZ).astimezone(dt.UTC)
    return start, now


def battery_energy_kwh(samples: list[Sample],
                       max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
                       ) -> tuple[float, float, float]:
    """(charged_kwh, discharged_kwh, skipped_s) integrated over `samples`.

    pbat is positive while discharging, so _accumulate's (import, export)
    buckets come back the other way round -- hence the swap on return. Reusing
    it rather than a fresh trapezoid loop is not tidiness: it splits an interval
    at the zero crossing, so a sample pair that flips from charge to discharge
    contributes to both totals instead of netting into one.

    A PAIR SEPARATED BY MORE THAN max_gap_s IS SKIPPED, not interpolated. The
    trapezoid between two samples assumes the power ramped between them, which
    is true across 30 s and a fabrication across an outage: six hours missing
    with the battery charging at 4 kW either side invents ~24 kWh of
    chargedToday out of nothing. pricing.compute_day() has the same shape and
    gets away with it because gate()'s max_gap_s check throws the whole day out
    afterwards -- there is no such second line of defence here, because a live
    figure is published the moment it is computed. Skipping under-reports the
    outage instead, which is the direction that cannot invent energy, and
    skipped_s says by how much it might have.
    """
    bucket = [0.0, 0.0]
    skipped_s = 0.0
    for a, b in zip(samples, samples[1:]):
        seconds = (b.time - a.time).total_seconds()
        if seconds > max_gap_s:
            skipped_s += seconds
            continue
        _accumulate(bucket, seconds / 3600.0, a.battery, b.battery)
    return bucket[1] / 1000.0, bucket[0] / 1000.0, skipped_s


# --------------------------------------------------------------------------
# InfluxDB reads
# --------------------------------------------------------------------------

_SAVING_TOTAL_FLUX = """
from(bucket: "{bucket}")
  |> range(start: 1970-01-01T00:00:00Z, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{meas}" and r.sys_sn == "{sys_sn}"
        and r.model_version == "{model_version}" and r._field == "saving")
  |> sum()
"""

_DISCHARGE_TOTAL_FLUX = """
from(bucket: "{bucket}")
  |> range(start: 1970-01-01T00:00:00Z, stop: {stop})
  |> filter(fn: (r) => r._measurement == "daily_energy" and r.sys_sn == "{sys_sn}"
        and r.model_version == "{model_version}" and r._field == "discharge_kwh_api")
  |> sum()
"""

# The local days daily_energy actually holds a row for, at the current version.
# One record per day -- a year of history is 365 rows, which is why the missing
# days can be derived client-side instead of by a second round of queries.
_DISCHARGE_DAYS_FLUX = """
from(bucket: "{bucket}")
  |> range(start: 1970-01-01T00:00:00Z, stop: {stop})
  |> filter(fn: (r) => r._measurement == "daily_energy" and r.sys_sn == "{sys_sn}"
        and r.model_version == "{model_version}" and r._field == "discharge_kwh_api")
  |> keep(columns: ["_time"])
  |> sort(columns: ["_time"])
"""


_NEWEST_SAMPLE_FLUX = """
from(bucket: "{bucket}")
  |> range(start: -{lookback})
  |> filter(fn: (r) => r._measurement == "{meas}" and r.sys_sn == "{sys_sn}"
        and r._field == "soc_percent")
  |> last()
"""


def newest_sample_time(query_api, bucket: str, sys_sn: str,
                       lookback: str = "30d") -> dt.datetime | None:
    """When power_readings last held anything, IGNORING the day window.

    Needed to tell an empty bucket from a dead collector. collect() looks only
    at today, so a collector that died at 22:00 and stayed dead leaves today's
    window empty from midnight -- and a verdict drawn from that window alone
    says "no data at all", i.e. fresh install / wrong sys_sn / unscoped token,
    at precisely the moment the outage is longest and the true answer is
    "stale". The diagnostic would invert exactly when it is most needed.
    """
    flux = _NEWEST_SAMPLE_FLUX.format(
        bucket=bucket, meas=pricing.POWER_MEASUREMENT, sys_sn=sys_sn, lookback=lookback)
    newest = None
    for table in query_api.query(flux):
        for rec in table.records:
            when = rec.get_time()
            if newest is None or when > newest:
                newest = when
    return newest


def _sum_query(query_api, flux: str) -> float:
    total = 0.0
    for table in query_api.query(flux):
        for rec in table.records:
            value = rec.get_value()
            if value is not None:
                total += float(value)
    return total


def stored_saving_total(query_api, bucket: str, sys_sn: str, stop: dt.datetime) -> float:
    """All-time euro saving from stored daily_cost rows, up to `stop`.

    Two known under-counts, both deliberate rather than papered over:
      * a day rejected by pricing.gate() is absent from daily_cost forever, and
        so from this total. Publishing an estimate for it would put a number on
        the leaderboard that no stored row can ever be reconciled against.
      * yesterday is missing until the nightly job runs (~02:00), so the total
        dips for the first couple of hours of each day and then recovers.
    """
    return _sum_query(query_api, _SAVING_TOTAL_FLUX.format(
        bucket=bucket, meas=pricing.DAILY_MEASUREMENT, sys_sn=sys_sn,
        model_version=MODEL_VERSION, stop=stop.isoformat(),
    ))


def stored_discharge_days(query_api, bucket: str, sys_sn: str,
                          stop: dt.datetime) -> list[dt.date]:
    """The local days daily_energy holds a discharge row for, oldest first."""
    flux = _DISCHARGE_DAYS_FLUX.format(
        bucket=bucket, sys_sn=sys_sn, model_version=ENERGY_MODEL_VERSION,
        stop=stop.isoformat())
    days = set()
    for table in query_api.query(flux):
        for rec in table.records:
            days.add(rec.get_time().astimezone(NL_TZ).date())
    return sorted(days)


def missing_discharge_days(stored: list[dt.date], until: dt.date) -> list[dt.date]:
    """Every local day between the first stored one and `until` with no row.

    Bounded below by the first stored day on purpose: power_readings may reach
    further back than daily_energy ever did, and filling that stretch would not
    repair a gap, it would silently redefine what the lifetime total covers.
    """
    if not stored:
        return []
    day, out = stored[0], []
    have = set(stored)
    while day <= until:
        if day not in have:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def discharge_from_readings(query_api, bucket: str, sys_sn: str, day: dt.date,
                            max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S) -> float:
    """One local day's kWh discharged, integrated from power_readings.

    Takes max_gap_s so a filled day and the live day in the same payload are
    integrated under one rule; a fill that quietly used the module default would
    disagree with today's own figures whenever the setting is tuned.
    """
    start, stop = pricing.day_window_utc(day)
    samples = pricing.load_samples_influx(query_api, bucket, sys_sn, start, stop)
    return battery_energy_kwh(samples, max_gap_s)[1]


def stored_discharge_total(query_api, bucket: str, sys_sn: str, stop: dt.datetime,
                           now: dt.datetime | None = None,
                           max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
                           max_fill_days: int = DEFAULT_MAX_FILL_DAYS) -> float:
    """All-time kWh discharged before `stop`, per AlphaESS's own daily totals,
    WITH ANY DAY daily_energy IS MISSING INTEGRATED OUT OF power_readings.

    daily_energy is written by the nightly job at ~03:00, so between midnight
    and then, yesterday's discharge is in no stored row -- while `discharged`
    for today has just reset to zero. Summing only what is stored would make
    totalBatteryCycles drop by about a full cycle every night and climb back
    three hours later.

    EVERY MISSING DAY, NOT JUST YESTERDAY. Filling only yesterday fixes the
    nightly dip and leaves a worse bug behind it: a day whose row never arrives
    -- one AlphaESS did not serve, one efficiency.gate() rejected -- is filled
    while it is yesterday and then dropped the following midnight, so the
    counter does not dip and recover, it steps down and stays there. A lifetime
    cycle counter that moves backwards is physically impossible, and anything
    reading it downstream is entitled to treat that as corrupt data rather than
    as a late batch job. On a healthy system this fills nothing or one day.

    batteryResultTotal has a comparable hole and keeps it (see
    stored_saving_total). The asymmetry is deliberate: a euro total that dips is
    a number moving, and the days it omits are days pricing.gate() judged
    unpublishable -- there is no physical law saying euros only go up.
    """
    total = _sum_query(query_api, _DISCHARGE_TOTAL_FLUX.format(
        bucket=bucket, sys_sn=sys_sn, model_version=ENERGY_MODEL_VERSION,
        stop=stop.isoformat(),
    ))
    now = dt.datetime.now(dt.UTC) if now is None else now
    yesterday = now.astimezone(NL_TZ).date() - dt.timedelta(days=1)
    missing = missing_discharge_days(
        stored_discharge_days(query_api, bucket, sys_sn, stop), yesterday)
    if len(missing) > max_fill_days:
        # Newest first: the recent days are the ones whose absence is still
        # moving the published figure. A backlog this size is a broken nightly
        # job, not a hiccup, and quietly issuing a query per day for it would
        # turn one refresh into a stampede.
        log.warning("daily_energy is missing %d days; filling only the newest %d. "
                    "totalBatteryCycles under-reports until the nightly job catches up",
                    len(missing), max_fill_days)
        missing = missing[-max_fill_days:]
    for day in missing:
        filled = discharge_from_readings(query_api, bucket, sys_sn, day, max_gap_s)
        log.info("daily_energy has no row for %s; filled %.2f kWh from power_readings "
                 "so the cycle count does not go backwards", day, filled)
        total += filled
    return total


_DAILY_BY_DAY_FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{meas}" and r.sys_sn == "{sys_sn}"
        and r.model_version == "{model_version}"
        and contains(value: r._field, set: [{fields}]))
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"])
"""


def _daily_rows(query_api, bucket: str, meas: str, sys_sn: str, model_version: str,
                fields: tuple[str, ...], start: dt.datetime,
                stop: dt.datetime) -> dict[dt.date, dict]:
    """{local date -> {field: value}} for one measurement over a window."""
    flux = _DAILY_BY_DAY_FLUX.format(
        bucket=bucket, meas=meas, sys_sn=sys_sn, model_version=model_version,
        fields=", ".join(f'"{f}"' for f in fields),
        start=start.isoformat(), stop=stop.isoformat(),
    )
    rows: dict[dt.date, dict] = {}
    for table in query_api.query(flux):
        for rec in table.records:
            day = rec.get_time().astimezone(NL_TZ).date()
            rows[day] = {f: float(rec.values[f]) for f in fields if rec.values.get(f) is not None}
    return rows


def energy_from_readings(query_api, bucket: str, sys_sn: str, day: dt.date,
                         max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
                         ) -> tuple[float, float]:
    """One local day's (charged, discharged) kWh, integrated from power_readings."""
    start, stop = pricing.day_window_utc(day)
    samples = pricing.load_samples_influx(query_api, bucket, sys_sn, start, stop)
    charged, discharged, _ = battery_energy_kwh(samples, max_gap_s)
    return charged, discharged


def month_days(year: int, month: int, until: dt.date) -> list[dt.date]:
    """Every local day of the month up to and including `until`.

    Capped at `until` -- the caller passes yesterday -- because these are
    FINISHED days. Today is still moving and is what /api/live is for; sending
    it here would publish a part-day as a whole one and then disagree with the
    live figure for the rest of the day.
    """
    day = dt.date(year, month, 1)
    out = []
    while day.month == month and day <= until:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def build_month(query_api, *, bucket: str, sys_sn: str, year: int, month: int,
                until: dt.date, max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
                ) -> tuple[list[dict], dict]:
    """(per-day figures, report) for one month of finished local days.

    Energy comes from daily_energy, and from power_readings for any day
    daily_energy has no row for -- the same repair the live path makes, for the
    same reason: those gaps are real (five days in August 2026) and a month
    total that silently omits them is wrong rather than incomplete.

    EUROS COME FROM STORED daily_cost ROWS ONLY. A day pricing.gate() rejected
    has no row, is reported as None here, and goes out as 0.00 flagged
    `invalid` rather than recomputed ungated. That is the same call
    stored_saving_total makes and for the same reason: an estimate on a public
    leaderboard is a number no stored row can ever be reconciled against. The
    API's own `invalid` flag is what says so without withholding the day.
    """
    days = month_days(year, month, until)
    if not days:
        return [], {"days": [], "filled": [], "unpriced": []}
    start = pricing.day_window_utc(days[0])[0]
    stop = pricing.day_window_utc(days[-1])[1]

    energy = _daily_rows(query_api, bucket, "daily_energy", sys_sn, ENERGY_MODEL_VERSION,
                         ("charge_kwh_api", "discharge_kwh_api"), start, stop)
    savings = _daily_rows(query_api, bucket, pricing.DAILY_MEASUREMENT, sys_sn,
                          MODEL_VERSION, ("saving",), start, stop)

    rows, filled, unpriced = [], [], []
    for day in days:
        row = energy.get(day)
        derived = False
        if row and "charge_kwh_api" in row and "discharge_kwh_api" in row:
            charged, discharged = row["charge_kwh_api"], row["discharge_kwh_api"]
        else:
            charged, discharged = energy_from_readings(
                query_api, bucket, sys_sn, day, max_gap_s)
            if not charged and not discharged:
                # Nothing stored and nothing measured: a day before the record
                # begins, or a total outage. Omitted entirely rather than sent
                # as zeros, which would read as "the battery did nothing" when
                # the truth is "we were not watching".
                continue
            derived = True
            filled.append(day)
        result = savings.get(day, {}).get("saving")
        if result is None:
            unpriced.append(day)
        rows.append({"day": day, "charged": charged, "discharged": discharged,
                     "result": result, "derived": derived})
    return rows, {"days": [r["day"] for r in rows], "filled": filled,
                  "unpriced": unpriced, "expected": len(days)}


def cycles(discharge_kwh: float, capacity_kwh: float, offset: float = 0.0) -> float:
    """Full-throughput-equivalent cycles: total kWh discharged / usable capacity.

    Not a count of charge/discharge events -- the platform's own integrations
    take the number off a vendor register that means throughput, and a partial
    daily cycle would otherwise count the same as a full one. `offset` carries
    whatever the pack did before this collector existed; without it the figure
    is "cycles since we started measuring" dressed up as a lifetime total.
    """
    if capacity_kwh <= 0:
        return offset
    return offset + discharge_kwh / capacity_kwh


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------

def _s(value: float, dp: int = 2) -> str:
    """Format a number the way this API wants it: as a STRING.

    Every numeric field in the spec is typed `string` -- batteryResult "8.80",
    chargedToday "11", totalBatteryCycles "143". /api/live happens to tolerate
    real JSON numbers, which is why this went unnoticed until
    /api/results/monthly refused a payload outright; matching the declared type
    removes the question.
    """
    return f"{value:.{dp}f}"


def grid_energy_kwh(samples: list[Sample],
                    max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S) -> tuple[float, float]:
    """(imported_kwh, exported_kwh) from the grid signal.

    pgrid is positive while importing, so _accumulate's buckets already line up.
    Integrated straight from power_readings rather than taken from
    compute_day(), so the two figures survive a day the price feed cannot cover
    -- they are measurements, and nothing about them depends on a price.
    """
    bucket = [0.0, 0.0]
    for a, b in zip(samples, samples[1:]):
        seconds = (b.time - a.time).total_seconds()
        if seconds > max_gap_s:
            continue
        _accumulate(bucket, seconds / 3600.0, a.grid, b.grid)
    return bucket[0] / 1000.0, bucket[1] / 1000.0


def pv_energy_kwh(samples: list[Sample],
                  max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S) -> float:
    """kWh generated by the panels. ppv is one-directional, so only the
    import bucket is ever filled."""
    bucket = [0.0, 0.0]
    for a, b in zip(samples, samples[1:]):
        seconds = (b.time - a.time).total_seconds()
        if seconds > max_gap_s:
            continue
        _accumulate(bucket, seconds / 3600.0, a.pv, b.pv)
    return bucket[0] / 1000.0


def build_payload(*, latest: Sample, charged_kwh: float, discharged_kwh: float,
                  result_today: float, result_total: float, cycle_count: float,
                  mode: str, load_balancing: bool,
                  imported_kwh: float = 0.0, exported_kwh: float = 0.0,
                  solar_kwh: float = 0.0, invalid: bool = False,
                  test: bool = False, charge_positive: bool = True) -> dict:
    """The POST /api/live body, per the published OpenAPI spec.

    Every numeric field is a string there, `loadBalancingActive` is 'on'/'off'
    rather than a boolean, and there is no `batteryPower` field at all -- the
    original payload here carried one, reverse-engineered from a third-party
    integration's example, and the API was simply ignoring it. `batteryCharge`
    is the state of charge and is the only battery-side instantaneous value the
    API takes, which is why charge_positive no longer changes what is sent.

    `batteryResult` is defined as batteryResultImbalance + batterySavings.
    Nothing here trades imbalance, so the two are the same number and both are
    sent: the saving against the no-battery counterfactual.
    """
    payload = {
        "timestamp": latest.time.astimezone(dt.UTC).isoformat(),
        "batteryResult": _s(result_today),
        "batteryResultTotal": _s(result_total),
        "batterySavings": _s(result_today),
        "batteryCharge": _s(latest.soc, 1),
        "chargedToday": _s(charged_kwh, 3),
        "dischargedToday": _s(discharged_kwh, 3),
        "gridImportToday": _s(imported_kwh, 3),
        "gridExportToday": _s(exported_kwh, 3),
        "solarKwhGenerated": _s(solar_kwh, 3),
        "totalBatteryCycles": _s(cycle_count),
        "loadBalancingActive": "on" if load_balancing else "off",
    }
    if invalid:
        # The spec has a field for exactly the days this service already knows
        # are shaky -- a gap it refused to integrate across, or a price feed too
        # thin to price the day. Better to publish the number and flag it than
        # to publish it silently or withhold it.
        payload["invalid"] = True
    if mode:
        payload["mode"] = mode
    if test:
        payload["test"] = True
    return payload


def build_daily(*, day: dt.date, charged_kwh: float, discharged_kwh: float,
                result: float | None, mode: str = "", invalid: bool = False,
                test: bool = False, note: str = "") -> dict:
    """The POST /api/results/daily body for one finished local day.

    This is where per-day history goes. /api/results/monthly takes month TOTALS
    and has no per-day structure at all -- the `days` map this repo first sent
    was invented, which is what the endpoint refused.

    `finalized` is deliberately never set: the spec says a finalized result can
    never be updated again, and a day here can legitimately improve when its
    nightly daily_cost row lands.
    """
    payload = {
        "date": day.isoformat(),
        "batteryResult": _s(result or 0.0),
        "batterySavings": _s(result or 0.0),
        "chargedToday": _s(charged_kwh, 3),
        "dischargedToday": _s(discharged_kwh, 3),
    }
    if invalid or result is None:
        payload["invalid"] = True
    if note:
        payload["note"] = note
    if mode:
        payload["mode"] = mode
    if test:
        payload["test"] = True
    return payload


def build_month_totals(*, year: int, month: int, charged_kwh: float,
                       discharged_kwh: float, result: float, partial: bool,
                       mode: str = "", note: str = "") -> dict:
    """The POST /api/results/monthly body: totals only, no per-day breakdown."""
    payload = {
        "yearMonth": f"{year:04d}-{month:02d}",
        "batteryResult": _s(result),
        "batterySavings": _s(result),
        "batteryCharged": _s(charged_kwh, 3),
        "batteryDischarged": _s(discharged_kwh, 3),
    }
    if partial:
        # True when the month is not fully covered -- the record starts partway
        # in, days are missing, or the month is still running. Without it a
        # short month reads as a bad month rather than an incomplete one.
        payload["partial"] = True
    if note:
        payload["note"] = note
    if mode:
        payload["mode"] = mode
    return payload


class Snapshot:
    """One submission's inputs, kept together so run_once and run_loop build the
    payload the same way and the status point can record what was sent."""

    def __init__(self, payload: dict, latest: Sample, age_s: float, sample_count: int,
                 skipped_s: float = 0.0, price_coverage: float = 1.0):
        self.payload = payload
        self.latest = latest
        self.age_s = age_s
        self.sample_count = sample_count
        # Seconds of today that battery_energy_kwh refused to integrate across.
        # Non-zero means chargedToday/dischargedToday are under-reported by an
        # unknown amount, which is worth being able to see on a panel next to
        # the figure itself rather than inferring from a gap in power_readings.
        self.skipped_s = skipped_s
        # Fraction of today so far that market_price actually covers. Below
        # pricing.MIN_PRICE_COVERAGE, batteryResult was sent as 0 -- this is
        # what tells that zero apart from a break-even day.
        self.price_coverage = price_coverage


class Totals:
    """Cached all-time sums. See DEFAULT_TOTALS_TTL_S for why they are cached.

    KEYED ON day_start AS WELL AS ON TIME, and the day is what makes this
    correct rather than merely fresh. Both sums cover everything *before* today,
    so at the midnight rollover their meaning changes: what was "up to and
    including yesterday" becomes "up to the day before yesterday", and the
    caller adds today's own throughput on top. A cache warmed at 23:30 and still
    inside its TTL at 00:05 would hand back a total missing the whole of the day
    that just ended, while `discharged` for the new day has reset to ~0 --
    dropping totalBatteryCycles by a full day and, worse, skipping the fill in
    stored_discharge_total that exists precisely to stop that. A TTL alone
    cannot see a day boundary; this does.
    """

    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._saving = 0.0
        self._discharge = 0.0
        self._at = 0.0
        self._day_start: dt.datetime | None = None

    def get(self, query_api, bucket: str, sys_sn: str, day_start: dt.datetime,
            now_mono: float | None = None, max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
            ) -> tuple[float, float]:
        now_mono = time.monotonic() if now_mono is None else now_mono
        expired = not self._at or now_mono - self._at >= self.ttl_s
        rolled_over = self._day_start != day_start
        if expired or rolled_over:
            self._saving = stored_saving_total(query_api, bucket, sys_sn, day_start)
            self._discharge = stored_discharge_total(
                query_api, bucket, sys_sn, day_start, max_gap_s=max_gap_s)
            self._at = now_mono
            self._day_start = day_start
            log.debug("Totals refreshed (%s): saving=%.4f discharge=%.2f",
                      "day rollover" if rolled_over else "ttl",
                      self._saving, self._discharge)
        return self._saving, self._discharge


def collect(query_api, *, bucket: str, sys_sn: str, totals: Totals, config: dict,
            now: dt.datetime | None = None) -> tuple[Snapshot | None, str]:
    """Read today's samples and prices and build the payload.

    Returns (snapshot, outcome). A None snapshot comes with the reason it is
    None, and the three reasons are not interchangeable:

      no-data    power_readings holds nothing at all for this sys_sn -- a fresh
                 install, a wrong serial, a token that cannot read.
      stale      samples exist but the newest is older than stale_after_s: the
                 collector stopped.
      day-start  the day rolled over seconds ago and its first poll has not
                 landed. Benign, clears itself, and deliberately not a fault.

    The first two are fixed in different places, and a panel showing only
    "nothing was submitted" cannot tell you which you have. Note that the
    verdict is drawn from the newest sample ANYWHERE, not from today's window --
    see newest_sample_time for why that distinction is the whole point.
    """
    now = dt.datetime.now(dt.UTC) if now is None else now
    start, end = today_window(now)

    samples = pricing.load_samples_influx(query_api, bucket, sys_sn, start, end)
    if not samples:
        # An empty day window is three different things, and they are told apart
        # by the newest sample ANYWHERE, not by the window that is empty.
        newest = newest_sample_time(query_api, bucket, sys_sn)
        if newest is None:
            log.warning("power_readings holds nothing for sys_sn=%s -- check the "
                        "serial and that the token can read the bucket", sys_sn)
            return None, "no-data"
        age = (now - newest).total_seconds()
        if age > config["stale_after_s"]:
            log.warning("No samples today; newest anywhere is %.0fs old -- the "
                        "collector has been down since before midnight", age)
            return None, "stale"
        # Fresh sample, empty day: the first poll of the new day has not landed
        # yet. Roughly 30 s wide, so on a 300 s cycle it is hit about once every
        # ten days -- and it is a normal state, not a fault. Saying so keeps it
        # out of the heartbeat instead of raising one false alarm per fortnight.
        log.info("Day just rolled over and today's first sample (%.0fs old) is not "
                 "in the window yet; nothing to submit this cycle", age)
        return None, "day-start"

    latest = samples[-1]
    age = (now - latest.time).total_seconds()
    if age > config["stale_after_s"]:
        log.warning("Newest sample is %.0fs old (> %ds), skipping submission",
                    age, config["stale_after_s"])
        return None, "stale"

    charged, discharged, skipped_s = battery_energy_kwh(samples, config["max_gap_s"])
    imported, exported = grid_energy_kwh(samples, config["max_gap_s"])
    solar = pv_energy_kwh(samples, config["max_gap_s"])
    if skipped_s:
        log.warning("Skipped %.0fs of gaps > %.0fs; chargedToday/dischargedToday "
                    "under-report today by an unknown amount",
                    skipped_s, config["max_gap_s"])

    # Today's euro result, from the same model daily_cost will store tomorrow.
    # Prices come from the `market_price` rows refresh-prices.sh keeps ahead of
    # the clock.
    #
    # CHECKED FOR COVERAGE, not merely for presence. integrate_by_interval drops
    # energy in an interval it has no price for, so a feed that stopped at 08:00
    # does not fail loudly at 20:00 -- it quietly returns eight hours of saving
    # for a twenty-hour day, and publishes it. That is what
    # pricing.MIN_PRICE_COVERAGE exists to catch, and it is the one gate check a
    # partial day does NOT fail by construction: prices are published a day
    # ahead, so the elapsed part of today should be fully priced or something is
    # wrong. Short coverage sends 0 with a warning rather than a plausible
    # fraction of the truth, and price_coverage lands on the status point so the
    # zero can be told from a genuinely break-even day.
    intervals = pricing.load_prices_influx(query_api, bucket, start, end)
    local_day = start.astimezone(NL_TZ).date()
    elapsed_s = (end - start).total_seconds()
    price_coverage = (pricing.priced_seconds(intervals, start, end) / elapsed_s
                      if elapsed_s > 0 else 0.0)
    if price_coverage >= pricing.MIN_PRICE_COVERAGE:
        result_today = pricing.compute_day(samples, intervals, local_day)["saving"]
    else:
        result_today = 0.0
        log.warning("market_price covers only %.1f%% of %s so far (need %.1f%%) -- "
                    "batteryResult sent as 0 rather than a partial day's saving. "
                    "Check refresh-prices.sh",
                    price_coverage * 100, local_day, pricing.MIN_PRICE_COVERAGE * 100)

    saving_total, discharge_total = totals.get(query_api, bucket, sys_sn, start,
                                               max_gap_s=config["max_gap_s"])
    cycle_count = cycles(discharge_total + discharged,
                         config["capacity_kwh"], config["cycles_offset"])

    payload = build_payload(
        latest=latest, charged_kwh=charged, discharged_kwh=discharged,
        result_today=result_today, result_total=saving_total + result_today,
        cycle_count=cycle_count, mode=config["mode"],
        load_balancing=config["load_balancing"],
        imported_kwh=imported, exported_kwh=exported, solar_kwh=solar,
        # The two conditions this service already detects and previously only
        # logged: energy it refused to integrate across a gap, and a euro figure
        # suppressed for want of prices. The API has a field for it.
        invalid=bool(skipped_s) or price_coverage < pricing.MIN_PRICE_COVERAGE,
        test=config["test"],
        charge_positive=config["charge_positive"],
    )
    return Snapshot(payload, latest, age, len(samples), skipped_s,
                    price_coverage), "ok"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class SubmitError(RuntimeError):
    """A submission the platform rejected. Carries the status for the caller to
    decide on -- a 4xx is a payload bug and will fail identically next time, a
    5xx or a timeout is worth simply trying again in five minutes."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _post(session: requests.Session, api_key: str, path: str, payload: dict,
          base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """POST one payload to `path`. Returns the HTTP status, or raises SubmitError.

    The response body is logged on failure, not just the status: there is no
    published field-by-field reference for this API, so the validation message
    is the only description of the schema anyone has. That is not theoretical --
    it is how the mode field was diagnosed.
    """
    try:
        resp = session.post(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SubmitError(f"request failed: {exc}") from exc

    if resp.status_code >= 400:
        body = (resp.text or "")[:500]
        hint = " (check MIJNBATTERIJ_API_KEY)" if resp.status_code in (401, 403) else ""
        raise SubmitError(f"HTTP {resp.status_code}{hint}: {body}", resp.status_code)
    return resp.status_code


def submit(session: requests.Session, api_key: str, payload: dict,
           base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """POST one live payload."""
    return _post(session, api_key, LIVE_PATH, payload, base_url, timeout)


def submit_monthly(session: requests.Session, api_key: str, payload: dict,
                   base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """POST one month's totals. No per-day structure -- see build_month_totals."""
    return _post(session, api_key, MONTHLY_PATH, payload, base_url, timeout)


def submit_daily(session: requests.Session, api_key: str, payload: dict,
                 base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """POST one finished day's result."""
    return _post(session, api_key, DAILY_PATH, payload, base_url, timeout)


def fetch_rank(session: requests.Session, api_key: str, base_url: str = API_BASE,
               timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """GET /api/me -> {'overall_rank': .., 'provider_rank': ..}, empty if absent."""
    try:
        resp = session.get(
            f"{base_url}{ME_PATH}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SubmitError(f"request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise SubmitError(f"HTTP {resp.status_code}: {(resp.text or '')[:500]}",
                          resp.status_code)
    # Defensive about shape, not just about parsing. Nothing published
    # describes this response, so a list body, a null resultToday or a rank
    # rendered as a string are all possibilities -- and an AttributeError or
    # ValueError raised here is a crash in a service whose actual job is the
    # submission loop. An unreadable rank is worth a warning, never an outage.
    try:
        body = resp.json()
    except ValueError as exc:
        raise SubmitError(f"unparseable /api/me body: {exc}") from exc
    today = body.get("resultToday") if isinstance(body, dict) else None
    if not isinstance(today, dict):
        log.warning("/api/me carried no resultToday object (got %s)", type(today).__name__)
        return {}
    ranks = {}
    for key, field in (("overallRank", "overall_rank"), ("providerRank", "provider_rank")):
        value = today.get(key)
        if value is None:
            continue
        try:
            ranks[field] = float(value)
        except (TypeError, ValueError):
            log.warning("/api/me %s is not a number: %r", key, value)
    return ranks


# --------------------------------------------------------------------------
# InfluxDB writes (observability)
# --------------------------------------------------------------------------

def status_point(snapshot: Snapshot | None, sys_sn: str, outcome: str,
                 status_code: int | None, when: dt.datetime) -> Point:
    """One `mijnbatterij_submit` point, so a Grafana panel can show what the
    leaderboard is being told and whether it is being told anything at all."""
    point = (
        Point(STATUS_MEASUREMENT)
        .tag("sys_sn", sys_sn)
        .tag("outcome", outcome)
        .field("submitted", 1.0 if outcome == "ok" else 0.0)
        .time(when, WritePrecision.S)
    )
    if status_code is not None:
        point = point.field("status_code", float(status_code))
    if snapshot is not None:
        p = snapshot.payload
        # float() because the payload holds strings: the API types every
        # numeric field as one. InfluxDB wants numbers, so the conversion
        # happens here rather than the payload carrying two representations.
        for key, field in (
            ("batteryResult", "battery_result"),
            ("batteryResultTotal", "battery_result_total"),
            ("batteryCharge", "battery_charge"),
            ("chargedToday", "charged_today"),
            ("dischargedToday", "discharged_today"),
            ("gridImportToday", "grid_import_today"),
            ("gridExportToday", "grid_export_today"),
            ("solarKwhGenerated", "solar_kwh_generated"),
            ("totalBatteryCycles", "total_battery_cycles"),
        ):
            if key in p:
                point = point.field(field, float(p[key]))
        point = point.field("invalid", 1.0 if p.get("invalid") else 0.0)
        point = point.field("sample_age_s", round(snapshot.age_s, 1))
        point = point.field("sample_count", float(snapshot.sample_count))
        point = point.field("gap_skipped_s", round(snapshot.skipped_s, 1))
        point = point.field("price_coverage", round(snapshot.price_coverage, 4))
    return point


def rank_point(ranks: dict, sys_sn: str, when: dt.datetime) -> Point:
    point = Point(RANK_MEASUREMENT).tag("sys_sn", sys_sn).time(when, WritePrecision.S)
    for field, value in ranks.items():
        point = point.field(field, float(value))
    return point


def _write(write_api, bucket: str, point: Point) -> None:
    """Never let observability take the service down: a failed status write is
    strictly less important than the submission it describes."""
    try:
        write_api.write(bucket=bucket, record=point)
    except Exception as exc:  # deliberately swallowed -- see the docstring
        log.warning("InfluxDB write failed: %s", exc)


def send_heartbeat(url: str, status: str = "up", msg: str = "OK",
                   timeout: float = 5) -> None:
    """Ping a Kuma "Push" monitor. Never raises; an empty URL means not monitored.

    The query string is REBUILT, not appended to -- the same fix as
    `collector/collector.py:407` and `dispatch/heartbeat.py:22`, arrived at here
    the same way the other two were: from a log line. The push URL Kuma displays
    already carries `?status=up&msg=OK&ping=`, that whole string is what lands in
    `.env`, and `params=` appends rather than replaces, so Express sees
    `status=["up", "down"]`. That matches neither value. Every ping registers
    DOWN and the message renders as `[object Object]` -- including, and this is
    the part that matters, the `up` pings, so the monitor never goes green and
    the `down` push that was supposed to carry the platform's rejection message
    arrives saying nothing.

    Third occurrence of this bug in this repo. It survives review because the
    broken call looks more correct than the fix does.
    """
    if not url:
        return
    target = urlunsplit(urlsplit(url)._replace(query=urlencode({"status": status, "msg": msg})))
    try:
        requests.get(target, timeout=timeout)
    except Exception as exc:
        # Bare, like both siblings: a bad URL in .env raises out of urlsplit or
        # requests' own validation, and a monitoring convenience must never take
        # down the submission loop it is monitoring.
        log.warning("Heartbeat push failed: %s", exc)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _mode_env() -> str:
    """MIJNBATTERIJ_MODE, checked against the spec's enum.

    Empty = do not send the field, which is the default and lets the profile
    page's own Modus stand. A value outside MODES is refused here rather than
    at the API, where it comes back as a 400 every cycle for as long as it
    takes someone to read the log.
    """
    mode = _str_env("MIJNBATTERIJ_MODE", "")
    if mode and mode not in MODES:
        log.error("MIJNBATTERIJ_MODE=%r is not one of %s -- sending no mode and "
                  "letting the profile page decide", mode, ", ".join(MODES))
        return ""
    return mode


def load_config() -> dict:
    capacity = _num_env("BATTERY_CAPACITY_KWH", 0.0)
    if capacity <= 0:
        log.warning("BATTERY_CAPACITY_KWH unset -- totalBatteryCycles will be "
                    "the offset alone")
    return {
        "interval_s": _num_env("MIJNBATTERIJ_INTERVAL_SECONDS", DEFAULT_INTERVAL_S, int),
        "timeout_s": _num_env("MIJNBATTERIJ_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_S),
        "stale_after_s": _num_env("MIJNBATTERIJ_STALE_AFTER_SECONDS",
                                  DEFAULT_STALE_AFTER_S, int),
        "totals_ttl_s": _num_env("MIJNBATTERIJ_TOTALS_TTL_SECONDS", DEFAULT_TOTALS_TTL_S),
        "rank_interval_s": _num_env("MIJNBATTERIJ_RANK_INTERVAL_SECONDS", 3600, int),
        # Left empty in .env by default so the ceiling tracks POLL_INTERVAL_SECONDS
        # instead of being pinned to whatever 3x it was on the day it was written.
        "max_gap_s": _num_env("MIJNBATTERIJ_MAX_SAMPLE_GAP_S", DEFAULT_MAX_SAMPLE_GAP_S),
        # Set by --test: the API validates the payload and stores nothing, so
        # a new integration can be checked against the real endpoint rather
        # than against a guess at its schema.
        "test": False,
        "capacity_kwh": capacity,
        "cycles_offset": _num_env("MIJNBATTERIJ_CYCLES_OFFSET", 0.0),
        # Empty = do not send `mode` at all. Deliberately the default: the
        # profile page already holds it, and a guessed value is validated
        # server-side against a provider-specific set nobody has published.
        "mode": _mode_env(),
        "load_balancing": _bool_env("MIJNBATTERIJ_LOAD_BALANCING", False),
        "charge_positive": _bool_env("MIJNBATTERIJ_CHARGE_POSITIVE", True),
        "base_url": _str_env("MIJNBATTERIJ_BASE_URL", API_BASE).rstrip("/"),
    }


def run_once(query_api, write_api, session, *, bucket: str, sys_sn: str,
             api_key: str, config: dict, totals: Totals, dry_run: bool,
             verbose: bool = False) -> tuple[Snapshot | None, str]:
    """One cycle. Returns (snapshot, outcome); raises SubmitError if the platform
    rejected the submission.

    EVERY status point is written here, including the one for a rejection --
    the caller sees only the exception, and a rejection recorded without the
    figures that caused it is the one case the measurement exists for. Debugging
    a 422 from `submitted=0` alone means guessing what was in the body.
    """
    now = dt.datetime.now(dt.UTC)
    snapshot, outcome = collect(query_api, bucket=bucket, sys_sn=sys_sn, totals=totals,
                                config=config, now=now)
    if snapshot is None:
        _write(write_api, bucket, status_point(None, sys_sn, outcome, None, now))
        return None, outcome
    if verbose:
        print(json.dumps(snapshot.payload, indent=2))
    if dry_run:
        log.info("Dry run: not submitting")
        return snapshot, "dry-run"
    try:
        status = submit(session, api_key, snapshot.payload,
                        base_url=config["base_url"], timeout=config["timeout_s"])
    except SubmitError as exc:
        _write(write_api, bucket, status_point(
            snapshot, sys_sn, "rejected" if exc.status else "unreachable",
            exc.status, now))
        raise
    _write(write_api, bucket, status_point(snapshot, sys_sn, "ok", status, now))
    p = snapshot.payload
    log.info("Submitted: soc=%s%% charged=%s kWh discharged=%s kWh grid +%s/-%s kWh "
             "result=€%s total=€%s%s",
             p["batteryCharge"], p["chargedToday"], p["dischargedToday"],
             p["gridImportToday"], p["gridExportToday"], p["batteryResult"],
             p["batteryResultTotal"], " [invalid]" if p.get("invalid") else "")
    return snapshot, "ok"


def run_backfill(query_api, write_api, session, *, bucket: str, sys_sn: str,
                 api_key: str, config: dict, months: list[tuple[int, int]],
                 dry_run: bool, now: dt.datetime | None = None) -> int:
    """Backfill finished days and months. Returns the number of failed posts.

    Two endpoints, in that order: each day to /api/results/daily, then the
    month's totals to /api/results/monthly. The monthly endpoint carries no
    per-day structure -- sending one is what earned a 400 -- so the daily posts
    are the history and the monthly post is the summary over it.

    One rejected day does not abandon the rest. This runs nightly and unattended
    (scripts/daily-mijnbatterij.sh), so a permanent 4xx on a single day -- or a
    429 landing halfway through thirty-odd POSTs -- would otherwise mean that
    every later day and the month totals are never sent, identically, every
    night. Each failure is logged and counted, and the count comes back so the
    caller can still exit nonzero and let DSM raise its task-failure
    notification: continuing is not the same as succeeding.
    """
    now = dt.datetime.now(dt.UTC) if now is None else now
    yesterday = now.astimezone(NL_TZ).date() - dt.timedelta(days=1)
    failures = 0
    for year, month in months:
        rows, report = build_month(
            query_api, bucket=bucket, sys_sn=sys_sn, year=year, month=month,
            until=yesterday, max_gap_s=config["max_gap_s"])
        label = f"{year:04d}-{month:02d}"
        if not rows:
            log.warning("%s: no finished days with data, nothing to send", label)
            continue

        charged = sum(r["charged"] for r in rows)
        discharged = sum(r["discharged"] for r in rows)
        result = sum(r["result"] or 0.0 for r in rows)
        # Partial when the month is not fully covered: days omitted for want of
        # any data, or days whose euro figure was suppressed. Without the flag a
        # short month reads as a bad month rather than an incomplete one.
        partial = len(rows) < report["expected"] or bool(report["unpriced"])

        log.info("%s: %d/%d days, charged %.1f kWh, discharged %.1f kWh, result €%.2f%s",
                 label, len(rows), report["expected"], charged, discharged, result,
                 " (partial)" if partial else "")
        if report["filled"]:
            log.info("%s: %d day(s) had no daily_energy row and were integrated from "
                     "power_readings: %s", label, len(report["filled"]),
                     ", ".join(str(d) for d in report["filled"]))
        if report["unpriced"]:
            # Named rather than counted: each is a day pricing.gate() threw out,
            # so each is worth knowing about on its own.
            log.warning("%s: %d day(s) have no stored daily_cost row, sent as €0.00 "
                        "flagged invalid: %s -- the month's euro total is short by "
                        "whatever those days were worth", label, len(report["unpriced"]),
                        ", ".join(str(d) for d in report["unpriced"]))

        day_payloads = [
            build_daily(day=r["day"], charged_kwh=r["charged"],
                        discharged_kwh=r["discharged"], result=r["result"],
                        mode=config["mode"], invalid=r["derived"], test=config["test"])
            for r in rows
        ]
        month_payload = build_month_totals(
            year=year, month=month, charged_kwh=charged, discharged_kwh=discharged,
            result=result, partial=partial, mode=config["mode"])

        if dry_run:
            print(json.dumps({"daily": day_payloads, "monthly": month_payload}, indent=2))
            log.info("%s: dry run, not submitting", label)
            continue

        sent = 0
        for payload in day_payloads:
            try:
                submit_daily(session, api_key, payload,
                             base_url=config["base_url"], timeout=config["timeout_s"])
            except SubmitError as exc:
                # Named, not counted: the point of continuing is that the log
                # says which day to go and look at.
                failures += 1
                log.error("%s: %s rejected, skipping it: %s",
                          label, payload["date"], exc)
            else:
                sent += 1
        log.info("%s: submitted %d/%d daily results", label, sent, len(day_payloads))

        # Sent even when days failed: the totals come from the stored month, not
        # from what the platform accepted, so they are still the right summary --
        # and `partial` already says the month is incomplete.
        try:
            status = submit_monthly(session, api_key, month_payload,
                                    base_url=config["base_url"], timeout=config["timeout_s"])
        except SubmitError as exc:
            failures += 1
            log.error("%s: month totals rejected: %s", label, exc)
            continue
        _write(write_api, bucket, status_point(
            None, sys_sn, "monthly-ok", status, dt.datetime.now(dt.UTC)))
        log.info("%s: submitted month totals (HTTP %d)", label, status)

    return failures


def run_loop(query_api, write_api, session, *, bucket: str, sys_sn: str,
             api_key: str, config: dict, max_cycles: int | None = None) -> None:
    """The submit loop. `max_cycles` exists so the failure paths below can be
    tested without a signal or a clock -- production never passes it."""
    running = True
    cycles_done = 0

    def stop(signum, _frame):
        nonlocal running
        log.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    interval = config["interval_s"]
    totals = Totals(config["totals_ttl_s"])
    heartbeat_url = os.environ.get("MIJNBATTERIJ_HEARTBEAT_URL", "")
    log.info("Submitting every %ds to %s (mode=%s, charge_positive=%s)",
             interval, config["base_url"],
             config["mode"] or "<from profile>", config["charge_positive"])

    consecutive_failures = 0
    next_rank = 0.0
    while running:
        started = time.monotonic()
        try:
            snapshot, outcome = run_once(
                query_api, write_api, session, bucket=bucket, sys_sn=sys_sn,
                api_key=api_key, config=config, totals=totals, dry_run=False)
            consecutive_failures = 0
            if snapshot is not None:
                send_heartbeat(heartbeat_url)
            elif outcome == "day-start":
                # The one benign empty cycle: the day rolled over seconds ago
                # and its first poll has not landed. It clears itself within one
                # poll interval, so it is neither a submission nor a fault.
                pass
            else:
                # NOT a heartbeat "up". Nothing reached the platform, and this
                # is the failure mode the monitor is there for: a collector
                # outage submits nothing for hours while every cycle completes
                # without error. Pushing up here would hold Kuma green through
                # exactly that. Not counted as a consecutive failure either --
                # backing off would not help, since the fault is upstream of
                # this service and retrying costs one cheap query.
                send_heartbeat(heartbeat_url, "down", f"nothing submitted: {outcome}")
        except SubmitError as exc:
            consecutive_failures += 1
            log.error("Submission failed (%d consecutive): %s", consecutive_failures, exc)
            # A 4xx other than 429 is a verdict on the payload, not a bad
            # moment: it will fail identically every 300 s until a setting or
            # the payload changes, and the retries only make the log longer.
            # Said once, on the first one, because the point of saying it is
            # that the reader stops waiting for it to clear -- e.g.
            # `400 Battery mode: self_consumption is not available for
            # frank-energie`, which is a .env edit and never resolves on its own.
            if consecutive_failures == 1 and exc.status and 400 <= exc.status < 500 \
                    and exc.status != 429:
                log.error("HTTP %d is a rejection of the payload, not a transient "
                          "fault -- it will repeat until a setting changes. If the "
                          "message names `mode`, set MIJNBATTERIJ_MODE in .env to a "
                          "value your control provider accepts and restart this "
                          "service; see DEPLOY.md, \"What `mode` should say\".",
                          exc.status)
            # The status point was written by run_once, with the payload that
            # was rejected still attached.
            #
            # Down from the second failure, matching the collector: one blip is
            # noise, a run of them is the thing worth waking up for.
            if consecutive_failures >= 2:
                send_heartbeat(heartbeat_url, "down", str(exc)[:200])
        except Exception as exc:
            consecutive_failures += 1
            log.exception("Submission cycle failed (%d consecutive)", consecutive_failures)
            # Leaves a trace, like both sibling paths. This is where an
            # unreachable InfluxDB lands -- collect() raises before there is
            # anything to submit -- and without a row the Grafana panel shows a
            # gap identical to the container simply not running, which is the
            # question mijnbatterij_submit exists to answer.
            _write(write_api, bucket, status_point(
                None, sys_sn, "error", None, dt.datetime.now(dt.UTC)))
            if consecutive_failures >= 2:
                send_heartbeat(heartbeat_url, "down", f"{type(exc).__name__}: {exc}"[:200])

        if config["rank_interval_s"] > 0 and time.monotonic() >= next_rank:
            # Bare `except Exception`, not `except SubmitError`. The rank is a
            # decoration on a Grafana panel; the submissions are the job. This
            # API has no published schema, so /api/me returning a list, a null
            # `resultToday`, or a rank as a string are all live possibilities,
            # and each raises something other than SubmitError -- which, from
            # out here, would leave the process and be restarted forever by
            # `restart: unless-stopped`, taking every submission down with it.
            try:
                ranks = fetch_rank(session, api_key, config["base_url"], config["timeout_s"])
                if ranks:
                    _write(write_api, bucket,
                           rank_point(ranks, sys_sn, dt.datetime.now(dt.UTC)))
                    log.info("Rank: %s", ranks)
            except SubmitError as exc:
                log.warning("Rank fetch failed: %s", exc)
            except Exception:
                log.exception("Rank fetch raised; continuing to submit")
            next_rank = time.monotonic() + config["rank_interval_s"]

        cycles_done += 1
        if max_cycles is not None and cycles_done >= max_cycles:
            break

        sleep_for = interval
        if consecutive_failures:
            sleep_for = min(interval * 2 ** min(consecutive_failures, 3), 3600)
        deadline = time.monotonic() + max(sleep_for - (time.monotonic() - started), 0)
        while running and time.monotonic() < deadline:
            time.sleep(1)

    log.info("Stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit battery figures to mijnbatterij.nl")
    parser.add_argument("--once", action="store_true",
                        help="Build one payload, print it, submit, and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and print the payload but do not submit")
    parser.add_argument("--test", action="store_true",
                        help="Submit for real but with the API's `test` flag set: it "
                             "validates the payload and stores nothing")
    parser.add_argument("--monthly", nargs="+", metavar="YYYY-MM",
                        help="Backfill finished months to /api/results/monthly and exit. "
                             "Only whole past days are sent; today belongs to --once.")
    args = parser.parse_args()
    months = []
    for value in args.monthly or []:
        try:
            when = dt.datetime.strptime(value, "%Y-%m")
        except ValueError:
            parser.error(f"--monthly takes YYYY-MM, not {value!r}")
        months.append((when.year, when.month))
    # A dry-run loop would be a container quietly doing nothing forever under
    # `restart: unless-stopped`, which is the shape of a service that looks
    # healthy and publishes nothing.
    args.once = args.once or (args.dry_run and not months)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Opt-in, like AWTRIX_HOST: the service ships in the base compose file, but
    # an installation that has not been registered on the platform has no key
    # and must idle rather than crash-loop under `restart: unless-stopped`.
    api_key = os.environ.get("MIJNBATTERIJ_API_KEY", "").strip()
    # Idle only when this is the long-running service. `--once` is someone at a
    # terminal waiting for an answer, and parking them in a sleep loop with a
    # message about idling reads as a hang -- they get the error instead.
    if not api_key and not args.once and not months:
        log.info("MIJNBATTERIJ_API_KEY not set; submission disabled. Idling.")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            time.sleep(3600)
    if not api_key and not args.dry_run:
        log.error("MIJNBATTERIJ_API_KEY is not set, so there is nothing to submit to. "
                  "Add --dry-run to build and print a payload without submitting.")
        sys.exit(1)

    bucket = env("INFLUX_BUCKET")
    sys_sn = env("ALPHAESS_SYS_SN")
    config = load_config()
    config["test"] = args.test

    # INFLUX_TOKEN_MIJNBATTERIJ is `:-` in docker-compose.yml rather than `:?`,
    # so that an unminted token cannot block every compose subcommand on the NAS
    # for a service nobody has enabled (see the comment there). The cost of that
    # choice is that an empty token arrives here instead of at `compose config`,
    # and an empty token queries nothing and writes nothing -- which without
    # this check looks exactly like a battery that never charges. Refuse to
    # start instead, naming the variable and where the recipe is.
    influx_token = env("INFLUX_TOKEN").strip()
    if not influx_token:
        # Reached by --dry-run with no API key as well as by the service proper,
        # so the message cannot claim the key is set.
        log.error("INFLUX_TOKEN_MIJNBATTERIJ is empty. Mint it per DEPLOY.md, "
                  "\"Scoped tokens\" -- without it this service can read neither "
                  "power_readings nor the daily totals, and cannot record what it "
                  "submitted.")
        sys.exit(1)

    client = InfluxDBClient(url=env("INFLUX_URL"), token=influx_token,
                            org=env("INFLUX_ORG"))
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    session = requests.Session()

    try:
        if months:
            failures = run_backfill(
                query_api, write_api, session, bucket=bucket, sys_sn=sys_sn,
                api_key=api_key, config=config, months=months, dry_run=args.dry_run)
            if failures:
                # The days that worked are already posted; this is the exit code
                # the nightly task's notification hangs off.
                log.error("%d submission(s) failed", failures)
                sys.exit(1)
        elif args.once:
            run_once(query_api, write_api, session, bucket=bucket, sys_sn=sys_sn,
                     api_key=api_key, config=config, totals=Totals(config["totals_ttl_s"]),
                     dry_run=args.dry_run, verbose=True)
        else:
            run_loop(query_api, write_api, session, bucket=bucket, sys_sn=sys_sn,
                     api_key=api_key, config=config)
    finally:
        client.close()


if __name__ == "__main__":
    main()
