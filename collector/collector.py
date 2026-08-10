"""AlphaESS -> InfluxDB collector.

Polls the AlphaESS Open API (getLastPowerData) on an interval and writes
power/SoC samples to InfluxDB.

Run modes:
    python collector.py          # poll loop (production)
    python collector.py --once   # single poll, print raw API response and
                                 # parsed fields, no InfluxDB write. Use this
                                 # to verify sign conventions for grid/battery.
"""

import hashlib
import logging
import os
import re
import signal
import socket
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

API_BASE = "https://openapi.alphaess.com/api"
API_HOST = urlsplit(API_BASE).hostname

# Must match the com.docker.network.driver.mtu driver_opt on alphaess-net in
# docker-compose.yml. Override with EXPECTED_MAX_MTU if that value changes.
DEFAULT_EXPECTED_MAX_MTU = 1400

# Consecutive poll failures before pushing a "down" heartbeat. A single failed
# poll is usually an upstream blip that the next poll rides out; pushing down
# on it would page for something already fixed. From the second failure on, the
# monitor's own grace period would trip anyway, so this only makes the alert
# arrive with a reason attached.
HEARTBEAT_DOWN_AFTER_FAILURES = 2

# Measurement recording poll failures and the outages they form. Separate from
# power_readings: those are the physical samples, and a gap in them is exactly
# what a failure is, so the explanation cannot live in the same series.
HEALTH_MEASUREMENT = "collector_health"

# Consecutive failures before running the network diagnosis. Late enough to
# rule out a blip (the API is briefly flaky most weeks), early enough that the
# verdict rides along with the alert rather than arriving after the outage.
DIAGNOSE_AFTER_FAILURES = 3

# Ceiling on the exponential backoff between failed polls.
#
# This is not only a politeness setting: it sets how long an outage silences
# the collector, and therefore how big a gap a run of failed polls leaves in
# power_readings. pricing.py excludes a whole day whose largest gap exceeds
# PRICING_MAX_GAP_S (1200 s by default), so a cap set too high converts a
# handful of failed polls into a lost day of savings data.
#
# At 120 s the gap after k consecutive failures is 30 + 60 + 120 * (k - 1):
# 570 s at five failures, and eleven failures before the 1200 s gate trips.
# The previous 300 s reached 1050 s at five failures and tripped the gate at
# six -- observed live on four days in 2026-07, each sitting one failed poll
# below the cliff. See MIGRATION.md, "Follow-ups this migration surfaced".
#
# Raise it only together with PRICING_MAX_GAP_S, and see the ladder in
# tests/test_collector_backoff.py before changing either.
DEFAULT_MAX_BACKOFF_S = 120

# Control endpoint for the diagnosis: unrelated to AlphaESS, and addressed by
# IP so that reaching it does not depend on DNS -- which is tested separately.
# Override with DIAGNOSTIC_URL where 1.1.1.1 is blocked.
DEFAULT_DIAGNOSTIC_URL = "https://1.1.1.1/"

# Kuma stores the push message and renders it into the notification; a phone
# notification has room for the part that identifies the fault, not for the
# full nested requests/urllib3/ssl chain.
MAX_ERROR_SUMMARY_CHARS = 160

# HTTPError messages from `requests` include the full request URL, which for
# this API carries sys_sn in the query string (?sysSn=...). error_summary
# ends up in the heartbeat message sent to Uptime Kuma, a third party -- strip
# the query string before it leaves the process.
_URL_QUERY_RE = re.compile(r"(https?://\S+?)\?\S*")

log = logging.getLogger("alphaess-collector")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def default_route_interface() -> str | None:
    """Name of the interface carrying the default route, per /proc/net/route.

    This is the only interface whose MTU matters for reaching the AlphaESS
    API. Checking every interface in the namespace is wrong: containers carry
    tunnel pseudo-devices (gre0, sit0, tunl0, ...) that always exist with
    MTUs above any sane cap, so a naive scan warns even when correctly
    configured. Returns None where procfs is unavailable (e.g. on macOS).
    """
    try:
        with open("/proc/net/route") as fh:
            lines = fh.readlines()[1:]  # skip header
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        # Destination 00000000 == 0.0.0.0, i.e. the default route.
        if len(fields) > 1 and fields[1] == "00000000":
            return fields[0]
    return None


def interface_mtu(name: str) -> int | None:
    """MTU of a named interface, or None if it cannot be read."""
    try:
        with open(f"/sys/class/net/{name}/mtu") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def check_mtu(expected_max: int) -> None:
    """Log the container's link MTU, warning if it exceeds the expected cap.

    Docker applies a network's driver_opts only when the network is first
    created. Editing com.docker.network.driver.mtu and re-running `up` leaves
    the old MTU in place silently -- `--force-recreate` does not help either,
    since it recreates the container but reuses the network. The stale, larger
    MTU then surfaces much later as intermittent
    "SSL: UNEXPECTED_EOF_WHILE_READING" errors against the AlphaESS API,
    because only the large TLS handshake packets get dropped.

    Logging the real MTU at startup turns that silent misconfiguration into
    something visible in `docker compose logs collector`.
    """
    iface = default_route_interface()
    if iface is None:
        log.info("Link MTU: no default route found, skipping check")
        return
    mtu = interface_mtu(iface)
    if mtu is None:
        log.info("Link MTU: could not read MTU of %s, skipping check", iface)
        return
    if mtu > expected_max:
        log.warning(
            "Link MTU %s=%d exceeds the expected maximum of %d. The docker "
            "network predates the driver_opts MTU cap in docker-compose.yml. "
            "Recreate it with `docker compose down && docker compose up -d` "
            "(--force-recreate is NOT enough -- it reuses the existing "
            "network). Expect intermittent TLS EOF errors until then.",
            iface, mtu, expected_max)
    else:
        log.info("Link MTU: %s=%d (expected <= %d)", iface, mtu, expected_max)


def resolve_host(host: str) -> tuple[bool, str]:
    """Resolve a hostname to addresses. Returns (ok, one-line description)."""
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"DNS: {host} did not resolve ({exc})"
    addresses = sorted({info[4][0] for info in infos})
    elapsed_ms = (time.monotonic() - started) * 1000
    return True, (f"DNS: {host} -> {', '.join(addresses)} "
                  f"({elapsed_ms:.0f}ms)")


def probe_control_url(url: str, timeout: float = 10) -> tuple[bool, str]:
    """Fetch an endpoint unrelated to AlphaESS. Returns (ok, description).

    Any HTTP status counts as success: the question is whether this container
    can complete DNS-free egress and a TLS handshake at all, not what the
    other end chose to answer.
    """
    started = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"Control request to {url} failed: {error_summary(exc)}"
    elapsed_ms = (time.monotonic() - started) * 1000
    return True, (f"Control request to {url}: HTTP {resp.status_code} "
                  f"({elapsed_ms:.0f}ms)")


def diagnose_network(expected_max_mtu: int, control_url: str) -> str:
    """Decide whether a run of poll failures is this host's fault or AlphaESS's.

    This is the question worth answering while an outage is happening, and the
    one that previously required walking to a computer: the failure modes are
    indistinguishable from the exception alone. A TLS EOF is the signature of
    an oversized container MTU (local, fixable now) *and* of an upstream edge
    dropping connections (nothing to do). Probing DNS and an unrelated HTTPS
    endpoint separates them.

    Returns a short slug for the alert message; the reasoning goes to the log.
    """
    log.warning("Diagnosing whether the fault is local or at the AlphaESS API:")
    check_mtu(expected_max_mtu)
    dns_ok, dns_line = resolve_host(API_HOST)
    log.warning("%s", dns_line)
    control_ok, control_line = probe_control_url(control_url)
    log.warning("%s", control_line)

    if not dns_ok:
        verdict, explanation = "local-dns", (
            "DNS for the API host is failing, so this is a local resolver or "
            "network problem, not AlphaESS. Check the container's DNS and the "
            "host's uplink.")
    elif not control_ok:
        verdict, explanation = "local-network", (
            "An unrelated HTTPS endpoint fails too, so this host's egress is "
            "the problem, not AlphaESS. Check the uplink first; if only TLS "
            "fails, check the link MTU logged above.")
    else:
        verdict, explanation = "upstream", (
            "DNS resolves and unrelated HTTPS works, so egress from this "
            "container is healthy and the fault is at the AlphaESS API. "
            "Nothing to fix here -- the collector keeps retrying and resumes "
            "on its own.")
    log.warning("Diagnosis (%s): %s", verdict, explanation)
    return verdict


def diagnose_write(influx_url: str) -> str:
    """Verdict for a run of failures on the InfluxDB write, not the API fetch.

    diagnose_network must not be used here. Its probes ask whether egress to
    the internet works, and InfluxDB is a compose service on this same host --
    so DNS and the control endpoint both succeed while writes keep failing, and
    it returns "upstream": "the fault is at the AlphaESS API. Nothing to fix
    here." That is exactly backwards, on the one message that decides whether
    the operator gets up. The API is very likely fine; what is broken is local
    and needs looking at.
    """
    log.warning(
        "Diagnosis (local-influxdb): the AlphaESS fetch succeeded and the "
        "InfluxDB write at %s failed, so samples are being collected and "
        "dropped. This is local: check `docker compose ps influxdb`, its "
        "logs, the token/org/bucket, and free disk on the volume.",
        influx_url)
    return "local-influxdb"


def auth_headers(app_id: str, app_secret: str) -> dict:
    timestamp = str(int(time.time()))
    sign = hashlib.sha512(f"{app_id}{app_secret}{timestamp}".encode()).hexdigest()
    return {
        "appId": app_id,
        "timeStamp": timestamp,
        "sign": sign,
        "Content-Type": "application/json",
    }


def get_last_power_data(app_id: str, app_secret: str, sys_sn: str) -> dict:
    """Fetch a real-time snapshot. Returns the `data` object of the response.

    Raises RuntimeError on transport errors or non-success API codes.
    """
    resp = requests.get(
        f"{API_BASE}/getLastPowerData",
        params={"sysSn": sys_sn},
        headers=auth_headers(app_id, app_secret),
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        raise RuntimeError(f"API error code={body.get('code')} msg={body.get('msg')}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected API response, no data object: {body}")
    return data


def format_duration(seconds: float) -> str:
    """Compact duration for log lines and heartbeat messages."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def backoff_seconds(interval: int, consecutive_failures: int,
                    max_backoff: float = DEFAULT_MAX_BACKOFF_S) -> float:
    """How long to wait before the next poll.

    `interval` while healthy; doubling per consecutive failure once one has
    happened, capped at `max_backoff`. The exponent stops at 4 so the cap is
    reached in a bounded number of steps regardless of `interval`.

    Pure, because the resulting gap in power_readings is what decides whether
    pricing.py keeps the day -- see DEFAULT_MAX_BACKOFF_S.
    """
    if not consecutive_failures:
        return interval
    return min(interval * 2 ** min(consecutive_failures, 4), max_backoff)


def gap_after_failures(interval: int, failures: int,
                       max_backoff: float = DEFAULT_MAX_BACKOFF_S) -> float:
    """Gap left in power_readings by `failures` failed polls, then a success.

    The loop subtracts each attempt's own duration from its sleep, so a cycle
    lasts exactly `backoff_seconds(...)` and the gap is their sum: one healthy
    interval to the first failed attempt, then one backoff per failure until
    the poll that succeeds.
    """
    return sum(backoff_seconds(interval, n, max_backoff)
               for n in range(failures + 1))


def error_summary(exc: Exception) -> str:
    """One-line description of a failure, for logs and heartbeat messages.

    requests wraps urllib3 wraps ssl, so `str(exc)` on a transport error runs
    to several hundred characters across the nested causes. The real fault is
    the innermost one, which requests renders as a trailing "(Caused by ...)"
    -- exactly the part a naive head-truncation would throw away.

    An HTTPError (unlike a connection error) carries the request URL straight
    in its message, query string and all -- redacted here before it can reach
    the heartbeat message or the logs.
    """
    detail = " ".join(str(exc).split())
    _, sep, cause = detail.partition("(Caused by ")
    if sep:
        detail = cause.rstrip(")")
    detail = _URL_QUERY_RE.sub(r"\1", detail)
    if len(detail) > MAX_ERROR_SUMMARY_CHARS:
        detail = detail[:MAX_ERROR_SUMMARY_CHARS] + "..."
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def write_health_event(write_api, bucket: str, sys_sn: str, event: str,
                       fields: dict, error_class: str | None = None,
                       stage: str | None = None) -> None:
    """Record a poll failure ("failure") or the end of an outage ("recovered").

    An outage is otherwise only visible in the container log: it rotates, it
    cannot be read from a phone, and it answers nothing about whether this has
    happened before. InfluxDB runs on the same host, so it stays reachable
    exactly when the AlphaESS API does not -- making it the one place an outage
    can be recorded while it is still happening, and read back through Grafana
    later.

    Successful polls write nothing here; `power_readings` arriving *is* the
    healthy signal, and duplicating it would double the write rate for no
    added information.

    Best-effort, like the heartbeat: bookkeeping about an outage must never
    turn into a second one.
    """
    point = Point(HEALTH_MEASUREMENT).tag("sys_sn", sys_sn).tag("event", event)
    if error_class:
        # Bounded set (ReadTimeout, SSLError, HTTPError, RuntimeError, ...),
        # so it is safe as a tag and lets Grafana group by failure mode.
        point = point.tag("error_class", error_class)
    if stage:
        # "fetch" or "write" -- which half of the poll broke. The exception
        # class alone does not say: a ReadTimeout is equally at home talking to
        # AlphaESS and to InfluxDB, and the two demand opposite responses.
        point = point.tag("stage", stage)
    for key, value in fields.items():
        point = point.field(key, value)
    try:
        write_api.write(bucket=bucket, record=point)
    except Exception as exc:
        log.warning("Health event write failed: %s", exc)


def recovery_message(failures: int, outage_seconds: float,
                     cause: str = "") -> str:
    """Heartbeat message for a successful poll.

    Kuma sends an "up" notification when pings resume but cannot say what the
    outage was or how long it lasted, so carry that in the message itself.

    `cause` -- the last failure, in the same "stage: summary [verdict]" shape
    the "down" message uses -- is carried too, because the "down" notification
    is the one that most often does not arrive. On 2026-08-10 three outages in
    twelve minutes all had the same cause, DNS resolution failing on the NAS,
    and Kuma could not send a single one of the "down" messages that said so:
    reaching Telegram means resolving api.telegram.org through the resolver
    that had just failed, so every send died with `getaddrinfo EAI_AGAIN` and
    Kuma has no retry queue for notifications. The three "up" messages arrived
    intact -- by then DNS was working again -- and said only "recovered after
    2 failures", which is the half of the story that cannot act on itself.

    That failure mode is not specific to DNS. Any outage of the link the NAS
    reaches the internet through takes out the notification channel and the
    thing being monitored together, and the "up" message is the one sent from
    the other side of it. So the message that survives by construction is the
    one that has to carry the diagnosis.
    """
    if not failures:
        return "OK"
    detail = f"; {cause}" if cause else ""
    return (f"OK (recovered after {failures} failures, "
            f"{format_duration(outage_seconds)}{detail})")


def send_heartbeat(url: str, status: str = "up", msg: str = "OK",
                   timeout: float = 5) -> None:
    """Ping a Kuma 'Push' monitor (a dead-man's switch for the whole
    collect->write path).

    `status` and `msg` are passed through to Kuma, which renders them into the
    notification. Without them an outage only ever reads "no ping received",
    which says nothing about whether the API, the network or InfluxDB broke --
    the difference between diagnosing from a phone and opening the container
    logs. Best-effort: never let a monitoring hiccup disturb collection, so all
    errors are swallowed.
    """
    if not url:
        return
    # The push URL Kuma displays already carries a query string
    # (?status=up&msg=OK&ping=), and that is what ends up in HEARTBEAT_URL.
    # Passing params= would *append* to it rather than replace it, and Express
    # parses repeated keys as arrays: status becomes ["up", "down"], which
    # matches neither value, so every ping registers as DOWN and the message
    # renders as "[object Object]". Rebuild the query instead of adding to it.
    target = urlunsplit(
        urlsplit(url)._replace(query=urlencode({"status": status, "msg": msg})))
    try:
        requests.get(target, timeout=timeout)
    except Exception as exc:
        log.warning("Heartbeat ping failed: %s", exc)


def parse_fields(data: dict) -> dict:
    """Extract the fields we store. All powers in watts.

    Sign conventions (per AlphaESS API):
      pgrid: positive = importing from grid, negative = exporting
      pbat:  positive = discharging battery, negative = charging
    Verify against a live response with --once before trusting dashboards.
    """
    fields = {
        "pv_power_w": data.get("ppv"),
        "grid_power_w": data.get("pgrid"),
        "load_power_w": data.get("pload"),
        "battery_power_w": data.get("pbat"),
        "soc_percent": data.get("soc"),
    }
    missing = [k for k, v in fields.items() if v is None]
    if missing:
        log.warning("API response missing fields: %s (raw keys: %s)",
                    missing, sorted(data.keys()))
    return {k: float(v) for k, v in fields.items() if v is not None}


def run_once(app_id: str, app_secret: str, sys_sn: str) -> None:
    import json
    data = get_last_power_data(app_id, app_secret, sys_sn)
    print("Raw API data object:")
    print(json.dumps(data, indent=2))
    print("\nParsed fields:")
    print(json.dumps(parse_fields(data), indent=2))


def run_loop(app_id: str, app_secret: str, sys_sn: str) -> None:
    influx_url = env("INFLUX_URL")
    influx_token = env("INFLUX_TOKEN")
    influx_org = env("INFLUX_ORG")
    influx_bucket = env("INFLUX_BUCKET")
    interval = int(env("POLL_INTERVAL_SECONDS", "30"))
    if interval < 10:
        log.warning("POLL_INTERVAL_SECONDS=%d below API floor of 10s, using 10", interval)
        interval = 10
    max_backoff = int(env("MAX_BACKOFF_SECONDS", str(DEFAULT_MAX_BACKOFF_S)))
    if max_backoff < interval:
        log.warning("MAX_BACKOFF_SECONDS=%d below the poll interval of %ds, using %d",
                    max_backoff, interval, interval)
        max_backoff = interval
    # Optional: URL of a Kuma "Push" monitor, pinged after each successful
    # write. Unset -> no heartbeat, collector behaves exactly as before.
    heartbeat_url = os.environ.get("HEARTBEAT_URL", "")
    expected_max_mtu = int(env("EXPECTED_MAX_MTU", str(DEFAULT_EXPECTED_MAX_MTU)))
    diagnostic_url = env("DIAGNOSTIC_URL", DEFAULT_DIAGNOSTIC_URL)
    check_mtu(expected_max_mtu)

    client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("Polling every %ds for sysSn=%s -> %s bucket=%s",
             interval, sys_sn, influx_url, influx_bucket)

    consecutive_failures = 0
    first_failure_at = 0.0
    verdict = ""
    # The last failure, kept alive past the iteration that raised it so the
    # recovery heartbeat can name it -- see recovery_message.
    last_failure = ""
    while running:
        started = time.monotonic()
        # Which half of the poll is in flight. An exception says what went
        # wrong but not where, and the two halves fail for unrelated reasons
        # with unrelated fixes -- see diagnose_write.
        stage = "fetch"
        try:
            data = get_last_power_data(app_id, app_secret, sys_sn)
            fields = parse_fields(data)
            if fields:
                point = Point("power_readings").tag("sys_sn", sys_sn)
                for key, value in fields.items():
                    point = point.field(key, value)
                stage = "write"
                write_api.write(bucket=influx_bucket, record=point)
                log.debug("Wrote point: %s", fields)
                # verdict is appended here rather than stored with the
                # failure: diagnose_network runs on a later poll than the one
                # that first failed, so at the moment last_failure is set it
                # does not exist yet.
                cause = last_failure
                if cause and verdict:
                    cause = f"{cause} [{verdict}]"
                send_heartbeat(heartbeat_url, msg=recovery_message(
                    consecutive_failures, started - first_failure_at, cause))
            else:
                log.warning("No usable fields in response, skipping write")
            # An outage that ends on its own is otherwise silent: the failures
            # simply stop, and nothing in the log says when, or for how long.
            if consecutive_failures:
                outage_seconds = started - first_failure_at
                log.info("Poll recovered after %d consecutive failures (%s)",
                         consecutive_failures, format_duration(outage_seconds))
                write_health_event(
                    write_api, influx_bucket, sys_sn, "recovered",
                    {"failures": consecutive_failures,
                     "outage_seconds": round(outage_seconds, 1)})
            consecutive_failures = 0
            verdict = ""
            last_failure = ""
        except Exception as exc:
            consecutive_failures += 1
            summary = error_summary(exc)
            last_failure = f"{stage}: {summary}"
            write_health_event(
                write_api, influx_bucket, sys_sn, "failure",
                {"failures": consecutive_failures, "error": summary},
                error_class=type(exc).__name__, stage=stage)
            if consecutive_failures == 1:
                first_failure_at = started
                log.exception("Poll failed at %s (1 consecutive)", stage)
            else:
                # A full traceback per poll buries a 15-minute outage in
                # hundreds of identical frames. The first one already carries
                # the stack; the rest only need to say what failed.
                log.error("Poll failed at %s (%d consecutive): %s",
                          stage, consecutive_failures, summary)
            # Runs once per outage, not on every failure: the answer cannot
            # change while the same run of failures continues, and the probes
            # should not become traffic of their own.
            if consecutive_failures == DIAGNOSE_AFTER_FAILURES:
                verdict = (diagnose_network(expected_max_mtu, diagnostic_url)
                           if stage == "fetch" else diagnose_write(influx_url))
            if consecutive_failures >= HEARTBEAT_DOWN_AFTER_FAILURES:
                # The verdict is the part that says whether to get up; the
                # stage is what says where to look, and is known from the
                # first failure rather than only from the third.
                suffix = f" [{verdict}]" if verdict else ""
                send_heartbeat(
                    heartbeat_url, status="down",
                    msg=f"{stage}: {summary} "
                        f"({consecutive_failures} consecutive failures)"
                        f"{suffix}")

        # Back off on repeated failures to avoid hammering the API, capped so
        # that an outage does not grow a gap big enough to cost a whole day of
        # savings data -- see DEFAULT_MAX_BACKOFF_S.
        sleep_for = backoff_seconds(interval, consecutive_failures, max_backoff)
        elapsed = time.monotonic() - started
        remaining = max(sleep_for - elapsed, 0)
        deadline = time.monotonic() + remaining
        while running and time.monotonic() < deadline:
            time.sleep(1)

    client.close()
    log.info("Stopped")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app_id = env("ALPHAESS_APP_ID")
    app_secret = env("ALPHAESS_APP_SECRET")
    sys_sn = env("ALPHAESS_SYS_SN")

    if "--once" in sys.argv:
        run_once(app_id, app_secret, sys_sn)
    else:
        run_loop(app_id, app_secret, sys_sn)


if __name__ == "__main__":
    main()
