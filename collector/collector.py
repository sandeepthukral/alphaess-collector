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


def send_heartbeat(url: str, timeout: float = 5) -> None:
    """Ping a Kuma 'Push' monitor after a successful write (a dead-man's switch
    for the whole collect->write path). Best-effort: never let a monitoring
    hiccup disturb collection, so all errors are swallowed."""
    if not url:
        return
    try:
        requests.get(url, timeout=timeout)
    except Exception as exc:
        log.debug("Heartbeat ping failed: %s", exc)


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
                send_heartbeat(heartbeat_url)
            else:
                log.warning("No usable fields in response, skipping write")
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            log.exception("Poll failed (%d consecutive)", consecutive_failures)
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
