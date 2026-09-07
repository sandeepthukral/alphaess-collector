"""One InfluxDB client for the whole controlpanel process.

`app.py` (querying the dashboard's mijnbatterij widget) and `audit.py` (writing the toggle
audit trail) both talk to the same InfluxDB instance under the same scoped
`INFLUX_TOKEN_CONTROLPANEL` -- there is no reason for each to open its own connection, and no
comment in either explained why they didn't share one.
"""
from __future__ import annotations

import os

from influxdb_client import InfluxDBClient

INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "alphaess")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN_CONTROLPANEL"]

client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
