"""InfluxDB -> AWTRIX 3 pusher.

Reads the latest power_readings sample already stored in InfluxDB (written by
the collector) and pushes a handful of custom apps to an AWTRIX 3 clock
(e.g. an Ulanzi TC001) over HTTP. It never calls the AlphaESS API — it only
reads what the collector has already persisted, so it adds zero load on the
upstream API and stays fully decoupled from the collector.

Apps pushed (each rotates in the AWTRIX loop):
    soc   Battery state of charge, prefixed +/- for charging/discharging
    pv    Solar generation
    grid  Grid power (green = exporting, red = importing)
    load  House load

Run modes:
    python pusher.py          # push loop (production)
    python pusher.py --once   # single read + push, verbose, then exit
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests
from influxdb_client import InfluxDBClient

log = logging.getLogger("awtrix-pusher")

# AWTRIX custom-app colours (RGB hex).
COLOR_PV = "#FFD400"      # solar: amber
COLOR_LOAD = "#00AAFF"    # load: blue
COLOR_EXPORT = "#00E000"  # grid exporting: green
COLOR_IMPORT = "#FF3030"  # grid importing: red
COLOR_IDLE = "#888888"    # near-zero / neutral
COLOR_STALE = "#555555"   # dim grey when data is stale

# SoC colour ramp thresholds (percent -> colour), high to low.
SOC_COLORS = [(60, "#00E000"), (30, "#FFD400"), (0, "#FF3030")]

# Battery/grid powers below this magnitude (watts) are treated as "idle" so we
# don't flicker a sign on sensor noise around zero.
IDLE_W = 30.0


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def fmt_power(watts: float) -> str:
    """Format a power in watts compactly: 1800 -> '1.8kW', 600 -> '600W'."""
    w = abs(watts)
    if w >= 1000:
        return f"{w / 1000:.1f}kW"
    return f"{round(w)}W"


def soc_color(soc: float) -> str:
    for threshold, color in SOC_COLORS:
        if soc >= threshold:
            return color
    return SOC_COLORS[-1][1]


def query_latest(client: InfluxDBClient, bucket: str) -> tuple[dict, datetime | None]:
    """Return (fields, newest_time) for the most recent power_readings sample.

    fields maps field name -> float. newest_time is the max _time across the
    returned fields (None if no data), used for the staleness check.
    """
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "power_readings")
  |> last()
'''
    tables = client.query_api().query(flux)
    fields: dict[str, float] = {}
    newest: datetime | None = None
    for table in tables:
        for record in table.records:
            fields[record.get_field()] = record.get_value()
            t = record.get_time()
            if t is not None and (newest is None or t > newest):
                newest = t
    return fields, newest


def build_apps(fields: dict, stale: bool, icons: dict[str, str]) -> dict[str, dict]:
    """Build {app_name: awtrix_payload} from the latest fields.

    icons maps app name (soc/pv/grid/load) -> AWTRIX icon (an icon name/ID that
    exists on the device, uploaded via the AWTRIX web UI icon manager). Empty
    string means no icon for that app (text-only).

    Sign conventions (from the collector / AlphaESS API):
      battery_power_w: positive = discharging, negative = charging
      grid_power_w:    positive = importing,   negative = exporting
    """
    pv = fields.get("pv_power_w")
    load = fields.get("load_power_w")
    grid = fields.get("grid_power_w")
    soc = fields.get("soc_percent")
    bat = fields.get("battery_power_w")

    apps: dict[str, dict] = {}

    def color(c: str) -> str:
        return COLOR_STALE if stale else c

    def app(name: str, label: str, value: str, c: str) -> dict:
        icon = icons.get(name)
        # The icon carries the identity, so drop the text label when one is set
        # to keep the value short enough to show without scrolling.
        text = value if icon else (f"{label} {value}" if label else value)
        payload = {"text": text, "color": color(c)}
        if icon:
            payload["icon"] = icon
        return payload

    if soc is not None:
        # +/- prefix indicates battery charging/discharging direction.
        sign = ""
        if bat is not None:
            if bat < -IDLE_W:
                sign = "+"   # charging
            elif bat > IDLE_W:
                sign = "-"   # discharging
        # Drop the decimal at a full 100% ("100%"); show one decimal otherwise.
        soc_text = f"{soc:.0f}" if soc >= 100 else f"{soc:.1f}"
        apps["soc"] = app("soc", "", f"{sign}{soc_text}%", soc_color(soc))

    if pv is not None:
        apps["pv"] = app("pv", "PV", fmt_power(pv), COLOR_PV)

    if grid is not None:
        if grid > IDLE_W:
            c = COLOR_IMPORT
        elif grid < -IDLE_W:
            c = COLOR_EXPORT
        else:
            c = COLOR_IDLE
        apps["grid"] = app("grid", "GRID", fmt_power(grid), c)

    if load is not None:
        apps["load"] = app("load", "LOAD", fmt_power(load), COLOR_LOAD)

    return apps


def push_app(base_url: str, name: str, payload: dict, timeout: float) -> None:
    resp = requests.post(
        f"{base_url}/api/custom",
        params={"name": name},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()


def push_all(base_url: str, apps: dict[str, dict], timeout: float) -> None:
    for name, payload in apps.items():
        push_app(base_url, name, payload, timeout)


def run_once(client: InfluxDBClient, bucket: str, base_url: str,
             stale_after: int, timeout: float, icons: dict[str, str],
             push: bool = True) -> None:
    import json
    fields, newest = query_latest(client, bucket)
    age = None if newest is None else (datetime.now(timezone.utc) - newest).total_seconds()
    stale = newest is None or (age is not None and age > stale_after)
    apps = build_apps(fields, stale, icons)
    print(f"Latest fields: {json.dumps(fields, indent=2)}")
    print(f"Newest point: {newest} (age {age}s, stale={stale})")
    print(f"AWTRIX apps: {json.dumps(apps, indent=2)}")
    if push:
        push_all(base_url, apps, timeout)
        print(f"Pushed {len(apps)} apps to {base_url}")


def run_loop(client: InfluxDBClient, bucket: str, base_url: str,
             interval: int, stale_after: int, timeout: float,
             icons: dict[str, str]) -> None:
    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("Pushing every %ds from bucket=%s -> %s (stale after %ds)",
             interval, bucket, base_url, stale_after)

    consecutive_failures = 0
    while running:
        started = time.monotonic()
        try:
            fields, newest = query_latest(client, bucket)
            if not fields:
                log.warning("No data in bucket %s, skipping push", bucket)
            else:
                age = None if newest is None else \
                    (datetime.now(timezone.utc) - newest).total_seconds()
                stale = newest is None or (age is not None and age > stale_after)
                if stale:
                    log.warning("Data is stale (age %.0fs > %ds), dimming display",
                                age or -1, stale_after)
                apps = build_apps(fields, stale, icons)
                push_all(base_url, apps, timeout)
                log.debug("Pushed %d apps: %s", len(apps), apps)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            log.exception("Push cycle failed (%d consecutive)", consecutive_failures)

        # Back off on repeated failures (e.g. clock unreachable), capped at 5 min.
        sleep_for = interval
        if consecutive_failures:
            sleep_for = min(interval * 2 ** min(consecutive_failures, 4), 300)
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + max(sleep_for - elapsed, 0)
        while running and time.monotonic() < deadline:
            time.sleep(1)

    client.close()
    log.info("Stopped")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    influx_url = env("INFLUX_URL")
    influx_token = env("INFLUX_TOKEN")
    influx_org = env("INFLUX_ORG")
    influx_bucket = env("INFLUX_BUCKET")

    # AWTRIX_HOST is optional: the pusher ships in the base compose file but the
    # feature is opt-in. If it's unset, idle quietly instead of crash-looping
    # under `restart: unless-stopped`.
    awtrix_host = os.environ.get("AWTRIX_HOST", "").strip()
    if not awtrix_host:
        log.info("AWTRIX_HOST not set; AWTRIX push disabled. Idling.")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            time.sleep(3600)
    base_url = awtrix_host if awtrix_host.startswith("http") else f"http://{awtrix_host}"
    base_url = base_url.rstrip("/")

    interval = int(env("PUSH_INTERVAL_SECONDS", "30"))
    stale_after = int(env("STALE_AFTER_SECONDS", "180"))
    timeout = float(env("AWTRIX_TIMEOUT_SECONDS", "5"))

    # Per-app AWTRIX icons (icon name/ID that exists on the device). Blank =
    # text-only for that app.
    icons = {
        "soc": os.environ.get("AWTRIX_ICON_SOC", "").strip(),
        "pv": os.environ.get("AWTRIX_ICON_PV", "").strip(),
        "grid": os.environ.get("AWTRIX_ICON_GRID", "").strip(),
        "load": os.environ.get("AWTRIX_ICON_LOAD", "").strip(),
    }

    client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)

    if "--once" in sys.argv:
        run_once(client, influx_bucket, base_url, stale_after, timeout, icons,
                 push="--no-push" not in sys.argv)
    else:
        run_loop(client, influx_bucket, base_url, interval, stale_after, timeout, icons)


if __name__ == "__main__":
    main()
