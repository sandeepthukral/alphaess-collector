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
import signal
import sys
import time

import requests
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

API_BASE = "https://openapi.alphaess.com/api"

# Must match the com.docker.network.driver.mtu driver_opt on alphaess-net in
# docker-compose.yml. Override with EXPECTED_MAX_MTU if that value changes.
DEFAULT_EXPECTED_MAX_MTU = 1400

# Consecutive poll failures before pushing a "down" heartbeat. A single failed
# poll is usually an upstream blip that the next poll rides out; pushing down
# on it would page for something already fixed. From the second failure on, the
# monitor's own grace period would trip anyway, so this only makes the alert
# arrive with a reason attached.
HEARTBEAT_DOWN_AFTER_FAILURES = 2

# Kuma stores the push message and renders it into the notification; a phone
# notification has room for the part that identifies the fault, not for the
# full nested requests/urllib3/ssl chain.
MAX_ERROR_SUMMARY_CHARS = 160

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


def error_summary(exc: Exception) -> str:
    """One-line description of a failure, for logs and heartbeat messages.

    requests wraps urllib3 wraps ssl, so `str(exc)` on a transport error runs
    to several hundred characters across the nested causes. The real fault is
    the innermost one, which requests renders as a trailing "(Caused by ...)"
    -- exactly the part a naive head-truncation would throw away.
    """
    detail = " ".join(str(exc).split())
    _, sep, cause = detail.partition("(Caused by ")
    if sep:
        detail = cause.rstrip(")")
    if len(detail) > MAX_ERROR_SUMMARY_CHARS:
        detail = detail[:MAX_ERROR_SUMMARY_CHARS] + "..."
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def recovery_message(failures: int, outage_seconds: float) -> str:
    """Heartbeat message for a successful poll.

    Kuma sends an "up" notification when pings resume but cannot say what the
    outage was or how long it lasted, so carry that in the message itself.
    """
    if not failures:
        return "OK"
    return (f"OK (recovered after {failures} failures, "
            f"{format_duration(outage_seconds)})")


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
    try:
        requests.get(url, params={"status": status, "msg": msg}, timeout=timeout)
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
    # Optional: URL of a Kuma "Push" monitor, pinged after each successful
    # write. Unset -> no heartbeat, collector behaves exactly as before.
    heartbeat_url = os.environ.get("HEARTBEAT_URL", "")
    expected_max_mtu = int(env("EXPECTED_MAX_MTU", str(DEFAULT_EXPECTED_MAX_MTU)))
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
    while running:
        started = time.monotonic()
        try:
            data = get_last_power_data(app_id, app_secret, sys_sn)
            fields = parse_fields(data)
            if fields:
                point = Point("power_readings").tag("sys_sn", sys_sn)
                for key, value in fields.items():
                    point = point.field(key, value)
                write_api.write(bucket=influx_bucket, record=point)
                log.debug("Wrote point: %s", fields)
                send_heartbeat(heartbeat_url, msg=recovery_message(
                    consecutive_failures, started - first_failure_at))
            else:
                log.warning("No usable fields in response, skipping write")
            # An outage that ends on its own is otherwise silent: the failures
            # simply stop, and nothing in the log says when, or for how long.
            if consecutive_failures:
                log.info("Poll recovered after %d consecutive failures (%s)",
                         consecutive_failures,
                         format_duration(started - first_failure_at))
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures == 1:
                first_failure_at = started
                log.exception("Poll failed (1 consecutive)")
            else:
                # A full traceback per poll buries a 15-minute outage in
                # hundreds of identical frames. The first one already carries
                # the stack; the rest only need to say what failed.
                log.error("Poll failed (%d consecutive): %s",
                          consecutive_failures, error_summary(exc))
            if consecutive_failures >= HEARTBEAT_DOWN_AFTER_FAILURES:
                send_heartbeat(
                    heartbeat_url, status="down",
                    msg=f"{error_summary(exc)} "
                        f"({consecutive_failures} consecutive failures)")
            # A TLS EOF is the signature of an oversized MTU on this network,
            # so re-run the check once the failures look persistent rather
            # than transient. Only on the 3rd failure: enough to rule out a
            # blip, and it does not repeat for the rest of the outage.
            if consecutive_failures == 3 and isinstance(exc, requests.exceptions.SSLError):
                log.warning("Repeated TLS failures against the AlphaESS API; "
                            "re-checking link MTU (a common cause):")
                check_mtu(expected_max_mtu)

        # Back off on repeated failures to avoid hammering the API,
        # capped at 5 minutes.
        sleep_for = interval
        if consecutive_failures:
            sleep_for = min(interval * 2 ** min(consecutive_failures, 4), 300)
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
