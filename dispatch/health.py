"""`collector_health` writes from the dispatch image. DESIGN-dispatch.md section 6.1.

A copy of `collector.write_health_event`'s write, not an import: this image carries
pymodbus and influxdb-client and nothing else -- `dispatch/heartbeat.py` says why --
and reaching across into `collector/` for one write is a poor reason to add a path
between two directories that ship as separate images. Keep the tag names in sync
with `collector/collector.py:write_health_event`: same measurement, same schema,
different process, so one Grafana panel and one alert rule cover all three.
"""
from __future__ import annotations

import logging

log = logging.getLogger("dispatch")

HEALTH_MEASUREMENT = "collector_health"


def write_heartbeat_failed(write_api, bucket: str, sys_sn: str, monitor: str, error: str) -> None:
    """Record a heartbeat push that could not be delivered.

    Best-effort, like the heartbeat itself: bookkeeping about a monitoring failure
    must never be able to raise into the control loop that is being monitored.
    `write_api` is None when INFLUX_URL/INFLUX_TOKEN are not configured -- the same
    degraded-not-fatal posture as `dispatch/state.py:StatePublisher` -- and that is
    silent here rather than logged, because `build_publisher` already said so once
    at startup.
    """
    if write_api is None:
        return
    from influxdb_client import Point

    point = (Point(HEALTH_MEASUREMENT)
             .tag("sys_sn", sys_sn)
             .tag("event", "heartbeat_failed")
             .tag("component", "dispatch")
             .tag("monitor", monitor)
             .tag("stage", "heartbeat")
             .field("error", error))
    try:
        write_api.write(bucket=bucket, record=point)
    except Exception as exc:
        log.warning("Health event write failed: %s", exc)
