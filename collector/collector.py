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

log = logging.getLogger("alphaess-collector")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


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
        except Exception:
            consecutive_failures += 1
            log.exception("Poll failed (%d consecutive)", consecutive_failures)

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
