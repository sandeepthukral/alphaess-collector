"""Audit trail for controlpanel actions.

Every dispatch-live toggle attempt -- successful or rejected -- is logged to stdout
(captured by the existing json-file logging driver) and written as a point to a
`controlpanel_audit` InfluxDB measurement, so a mistaken or malicious toggle has a
timestamped record in both places and the history is chartable in Grafana later.
"""
from __future__ import annotations

import logging
import os
import sys

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                     format="%(asctime)s %(levelname)s %(message)s",
                     stream=sys.stdout)
log = logging.getLogger("controlpanel")

INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "alphaess")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN_CONTROLPANEL"]

_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_write_api = _client.write_api(write_options=SYNCHRONOUS)


def log_dispatch_live_toggle(*, from_state: str, to_state: str, accepted: bool,
                              reason: str = "") -> None:
    """Record one live-toggle attempt. Called for both accepted and rejected attempts."""
    log.info("dispatch_live toggle: %s -> %s accepted=%s reason=%s",
              from_state, to_state, accepted, reason or "-")
    point = (
        Point("controlpanel_audit")
        .tag("action", "dispatch_live")
        .field("from_state", from_state)
        .field("to_state", to_state)
        .field("accepted", accepted)
        .field("reason", reason)
    )
    try:
        _write_api.write(bucket=INFLUX_BUCKET, record=point)
    except Exception:
        # The audit log must never be the reason a real toggle is lost or blocked -- the
        # stdout line above already captured it. An InfluxDB outage should not also take
        # down the control panel.
        log.exception("failed to write controlpanel_audit point (toggle itself still applied)")
