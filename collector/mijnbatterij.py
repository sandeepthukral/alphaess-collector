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

TWO THINGS ABOUT THIS API ARE NOT PUBLICLY SPECIFIED, and both are settings
here rather than assumptions baked into the payload:

  * the sign convention for `batteryPower`. AlphaESS's pbat is positive while
    DISCHARGING; this sends charge-positive by default
    (MIJNBATTERIJ_CHARGE_POSITIVE=1), which is the usual convention for a
    battery dashboard sitting next to a "charged today" figure. If the
    platform's graph reads inverted, flip the setting -- do not edit the sign
    in here, or the next reader has to rediscover why it disagrees with
    power_readings.
  * `mode`. "self_consumption" | "self_consumption_plus" | "imbalance" are the
    documented values; a day-ahead-price-driven DIY dispatcher is closest to
    self_consumption_plus, but nothing here can verify how the platform buckets
    it, so MIJNBATTERIJ_MODE decides and defaults to the conservative one.

Run modes:
    python mijnbatterij.py           # submit loop (production)
    python mijnbatterij.py --once    # build one payload, print it, submit
    python mijnbatterij.py --once --dry-run   # ... and do not submit
"""

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import time

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import pricing
from prices import NL_TZ
from pricing import Sample, _accumulate

log = logging.getLogger("mijnbatterij")

API_BASE = "https://api.mijnbatterij.nl"
LIVE_PATH = "/api/live"
ME_PATH = "/api/me"

STATUS_MEASUREMENT = "mijnbatterij_submit"
RANK_MEASUREMENT = "mijnbatterij_rank"

# The daily_cost model whose savings are summed into batteryResultTotal. Pinned
# to whatever pricing.py currently writes, so a model bump moves both together
# rather than silently mixing two models' euros in one total.
MODEL_VERSION = pricing.MODEL_VERSION

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


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


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


def battery_energy_kwh(samples: list[Sample]) -> tuple[float, float]:
    """(charged_kwh, discharged_kwh) integrated over `samples`.

    pbat is positive while discharging, so _accumulate's (import, export)
    buckets come back the other way round -- hence the swap on return. Reusing
    it rather than a fresh trapezoid loop is not tidiness: it splits an interval
    at the zero crossing, so a sample pair that flips from charge to discharge
    contributes to both totals instead of netting into one.
    """
    bucket = [0.0, 0.0]
    for a, b in zip(samples, samples[1:]):
        hours = (b.time - a.time).total_seconds() / 3600.0
        _accumulate(bucket, hours, a.battery, b.battery)
    return bucket[1] / 1000.0, bucket[0] / 1000.0


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
        and r._field == "discharge_kwh_api")
  |> sum()
"""


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


def stored_discharge_total(query_api, bucket: str, sys_sn: str, stop: dt.datetime) -> float:
    """All-time kWh discharged, per AlphaESS's own daily totals."""
    return _sum_query(query_api, _DISCHARGE_TOTAL_FLUX.format(
        bucket=bucket, sys_sn=sys_sn, stop=stop.isoformat(),
    ))


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

def build_payload(*, latest: Sample, charged_kwh: float, discharged_kwh: float,
                  result_today: float, result_total: float, cycle_count: float,
                  mode: str, load_balancing: bool,
                  charge_positive: bool = True) -> dict:
    """The POST /api/live body. Pure: everything it needs is an argument."""
    power = -latest.battery if charge_positive else latest.battery
    return {
        "timestamp": latest.time.astimezone(dt.UTC).isoformat(),
        "batteryResult": round(result_today, 4),
        "batteryResultTotal": round(result_total, 4),
        "batteryCharge": round(latest.soc, 1),
        "batteryPower": round(power),
        "chargedToday": round(charged_kwh, 3),
        "dischargedToday": round(discharged_kwh, 3),
        "totalBatteryCycles": round(cycle_count, 2),
        "mode": mode,
        "loadBalancingActive": load_balancing,
    }


class Snapshot:
    """One submission's inputs, kept together so run_once and run_loop build the
    payload the same way and the status point can record what was sent."""

    def __init__(self, payload: dict, latest: Sample, age_s: float, sample_count: int):
        self.payload = payload
        self.latest = latest
        self.age_s = age_s
        self.sample_count = sample_count


class Totals:
    """Cached all-time sums. See DEFAULT_TOTALS_TTL_S for why they are cached."""

    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._saving = 0.0
        self._discharge = 0.0
        self._at = 0.0

    def get(self, query_api, bucket: str, sys_sn: str, day_start: dt.datetime,
            now_mono: float | None = None) -> tuple[float, float]:
        now_mono = time.monotonic() if now_mono is None else now_mono
        if not self._at or now_mono - self._at >= self.ttl_s:
            self._saving = stored_saving_total(query_api, bucket, sys_sn, day_start)
            self._discharge = stored_discharge_total(query_api, bucket, sys_sn, day_start)
            self._at = now_mono
            log.debug("Totals refreshed: saving=%.4f discharge=%.2f",
                      self._saving, self._discharge)
        return self._saving, self._discharge


def collect(query_api, *, bucket: str, sys_sn: str, totals: Totals, config: dict,
            now: dt.datetime | None = None) -> Snapshot | None:
    """Read today's samples and prices and build the payload. None if there is
    nothing worth sending (no samples at all, or the newest one is stale)."""
    now = dt.datetime.now(dt.UTC) if now is None else now
    start, end = today_window(now)

    samples = pricing.load_samples_influx(query_api, bucket, sys_sn, start, end)
    if not samples:
        log.warning("No power_readings since %s, nothing to submit", start.isoformat())
        return None

    latest = samples[-1]
    age = (now - latest.time).total_seconds()
    if age > config["stale_after_s"]:
        log.warning("Newest sample is %.0fs old (> %ds), skipping submission",
                    age, config["stale_after_s"])
        return None

    charged, discharged = battery_energy_kwh(samples)

    # Today's euro result, from the same model daily_cost will store tomorrow.
    # Prices come from the `market_price` rows refresh-prices.sh keeps ahead of
    # the clock; an unpriced stretch contributes zero to both worlds, so a
    # missing price feed shows as a flat result rather than a wrong one.
    intervals = pricing.load_prices_influx(query_api, bucket, start, end)
    local_day = start.astimezone(NL_TZ).date()
    result_today = pricing.compute_day(samples, intervals, local_day)["saving"] \
        if intervals else 0.0
    if not intervals:
        log.warning("No market_price rows for %s -- batteryResult sent as 0", local_day)

    saving_total, discharge_total = totals.get(query_api, bucket, sys_sn, start)
    cycle_count = cycles(discharge_total + discharged,
                         config["capacity_kwh"], config["cycles_offset"])

    payload = build_payload(
        latest=latest, charged_kwh=charged, discharged_kwh=discharged,
        result_today=result_today, result_total=saving_total + result_today,
        cycle_count=cycle_count, mode=config["mode"],
        load_balancing=config["load_balancing"],
        charge_positive=config["charge_positive"],
    )
    return Snapshot(payload, latest, age, len(samples))


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


def submit(session: requests.Session, api_key: str, payload: dict,
           base_url: str = API_BASE, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """POST one live payload. Returns the HTTP status; raises SubmitError otherwise.

    The response body is logged on failure, not just the status: there is no
    published field-by-field reference for this API, so the validation message
    is the only description of the schema anyone has.
    """
    try:
        resp = session.post(
            f"{base_url}{LIVE_PATH}",
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
    try:
        today = (resp.json() or {}).get("resultToday") or {}
    except ValueError as exc:
        raise SubmitError(f"unparseable /api/me body: {exc}") from exc
    ranks = {}
    for key, field in (("overallRank", "overall_rank"), ("providerRank", "provider_rank")):
        value = today.get(key)
        if value is not None:
            ranks[field] = float(value)
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
        for key, field in (
            ("batteryResult", "battery_result"),
            ("batteryResultTotal", "battery_result_total"),
            ("batteryCharge", "battery_charge"),
            ("batteryPower", "battery_power"),
            ("chargedToday", "charged_today"),
            ("dischargedToday", "discharged_today"),
            ("totalBatteryCycles", "total_battery_cycles"),
        ):
            point = point.field(field, float(p[key]))
        point = point.field("sample_age_s", round(snapshot.age_s, 1))
        point = point.field("sample_count", float(snapshot.sample_count))
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


def send_heartbeat(url: str, status: str = "up", msg: str = "OK") -> None:
    if not url:
        return
    try:
        requests.get(url, params={"status": status, "msg": msg, "ping": ""}, timeout=5)
    except requests.RequestException as exc:
        log.warning("Heartbeat push failed: %s", exc)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def load_config() -> dict:
    capacity = float(env("BATTERY_CAPACITY_KWH", "0") or 0)
    if capacity <= 0:
        log.warning("BATTERY_CAPACITY_KWH unset -- totalBatteryCycles will be "
                    "the offset alone")
    return {
        "interval_s": int(env("MIJNBATTERIJ_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_S))),
        "timeout_s": float(env("MIJNBATTERIJ_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_S))),
        "stale_after_s": int(env("MIJNBATTERIJ_STALE_AFTER_SECONDS",
                                 str(DEFAULT_STALE_AFTER_S))),
        "totals_ttl_s": float(env("MIJNBATTERIJ_TOTALS_TTL_SECONDS",
                                  str(DEFAULT_TOTALS_TTL_S))),
        "rank_interval_s": int(env("MIJNBATTERIJ_RANK_INTERVAL_SECONDS", "3600")),
        "capacity_kwh": capacity,
        "cycles_offset": float(env("MIJNBATTERIJ_CYCLES_OFFSET", "0") or 0),
        "mode": env("MIJNBATTERIJ_MODE", "self_consumption"),
        "load_balancing": _bool_env("MIJNBATTERIJ_LOAD_BALANCING", False),
        "charge_positive": _bool_env("MIJNBATTERIJ_CHARGE_POSITIVE", True),
        "base_url": env("MIJNBATTERIJ_BASE_URL", API_BASE).rstrip("/"),
    }


def run_once(query_api, write_api, session, *, bucket: str, sys_sn: str,
             api_key: str, config: dict, totals: Totals, dry_run: bool,
             verbose: bool = False) -> Snapshot | None:
    now = dt.datetime.now(dt.UTC)
    snapshot = collect(query_api, bucket=bucket, sys_sn=sys_sn, totals=totals,
                       config=config, now=now)
    if snapshot is None:
        _write(write_api, bucket, status_point(None, sys_sn, "no-data", None, now))
        return None
    if verbose:
        print(json.dumps(snapshot.payload, indent=2))
    if dry_run:
        log.info("Dry run: not submitting")
        return snapshot
    status = submit(session, api_key, snapshot.payload,
                    base_url=config["base_url"], timeout=config["timeout_s"])
    _write(write_api, bucket, status_point(snapshot, sys_sn, "ok", status, now))
    log.info("Submitted: soc=%.1f%% power=%dW charged=%.2fkWh discharged=%.2fkWh "
             "result=€%.4f total=€%.2f",
             snapshot.payload["batteryCharge"], snapshot.payload["batteryPower"],
             snapshot.payload["chargedToday"], snapshot.payload["dischargedToday"],
             snapshot.payload["batteryResult"], snapshot.payload["batteryResultTotal"])
    return snapshot


def run_loop(query_api, write_api, session, *, bucket: str, sys_sn: str,
             api_key: str, config: dict) -> None:
    running = True

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
             interval, config["base_url"], config["mode"], config["charge_positive"])

    consecutive_failures = 0
    next_rank = 0.0
    while running:
        started = time.monotonic()
        try:
            run_once(query_api, write_api, session, bucket=bucket, sys_sn=sys_sn,
                     api_key=api_key, config=config, totals=totals, dry_run=False)
            consecutive_failures = 0
            send_heartbeat(heartbeat_url)
        except SubmitError as exc:
            consecutive_failures += 1
            log.error("Submission failed (%d consecutive): %s", consecutive_failures, exc)
            _write(write_api, bucket, status_point(
                None, sys_sn, "rejected" if exc.status else "unreachable",
                exc.status, dt.datetime.now(dt.UTC)))
            # Down from the second failure, matching the collector: one blip is
            # noise, a run of them is the thing worth waking up for.
            if consecutive_failures >= 2:
                send_heartbeat(heartbeat_url, "down", str(exc)[:200])
        except Exception:
            consecutive_failures += 1
            log.exception("Submission cycle failed (%d consecutive)", consecutive_failures)

        if config["rank_interval_s"] > 0 and time.monotonic() >= next_rank:
            try:
                ranks = fetch_rank(session, api_key, config["base_url"], config["timeout_s"])
                if ranks:
                    _write(write_api, bucket,
                           rank_point(ranks, sys_sn, dt.datetime.now(dt.UTC)))
                    log.info("Rank: %s", ranks)
            except SubmitError as exc:
                log.warning("Rank fetch failed: %s", exc)
            next_rank = time.monotonic() + config["rank_interval_s"]

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
                        help="Build and print one payload but do not submit (implies --once)")
    args = parser.parse_args()
    # A dry-run loop would be a container quietly doing nothing forever under
    # `restart: unless-stopped`, which is the shape of a service that looks
    # healthy and publishes nothing.
    args.once = args.once or args.dry_run

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Opt-in, like AWTRIX_HOST: the service ships in the base compose file, but
    # an installation that has not been registered on the platform has no key
    # and must idle rather than crash-loop under `restart: unless-stopped`.
    api_key = os.environ.get("MIJNBATTERIJ_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        log.info("MIJNBATTERIJ_API_KEY not set; submission disabled. Idling.")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            time.sleep(3600)

    bucket = env("INFLUX_BUCKET")
    sys_sn = env("ALPHAESS_SYS_SN")
    config = load_config()

    client = InfluxDBClient(url=env("INFLUX_URL"), token=env("INFLUX_TOKEN"),
                            org=env("INFLUX_ORG"))
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)
    session = requests.Session()

    try:
        if args.once:
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
