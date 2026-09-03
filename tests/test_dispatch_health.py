"""`dispatch/health.py:write_heartbeat_failed`. TODO.md #18, DESIGN-dispatch.md section 6.

A copy of `collector.write_health_event`'s write, kept to one function and tested here
because the schema (measurement, tag names) has to stay in sync with the collector's copy
by hand -- nothing enforces that except this test asserting the shape.
"""
from __future__ import annotations

import health as H


class FakeWriteApi:
    def __init__(self):
        self.points = []

    def write(self, bucket, record):
        self.points.append((bucket, record))


def test_records_the_expected_tags_and_field():
    api = FakeWriteApi()
    H.write_heartbeat_failed(api, "alphaess", "SN123", monitor="dispatch-confirmed",
                             error="connection refused")

    assert len(api.points) == 1
    bucket, point = api.points[0]
    assert bucket == "alphaess"
    line = point.to_line_protocol()
    assert line.startswith("collector_health,")
    assert "sys_sn=SN123" in line
    assert "event=heartbeat_failed" in line
    assert "component=dispatch" in line
    assert "monitor=dispatch-confirmed" in line
    assert 'error="connection refused"' in line


def test_a_none_write_api_is_a_silent_noop():
    """Matches StatePublisher's degraded-not-fatal posture: Influx not configured means
    nowhere to write, not an error -- build_publisher already logged that once at startup."""
    H.write_heartbeat_failed(None, "alphaess", "SN123", monitor="soc-floor", error="timeout")


def test_a_write_failure_is_swallowed():
    """Bookkeeping about a monitoring failure must never be able to raise into the loop
    that is being monitored -- the same posture as every other publisher in this module."""
    class BoomWriteApi:
        def write(self, bucket, record):
            raise OSError("influx unreachable")

    H.write_heartbeat_failed(BoomWriteApi(), "alphaess", "SN123",
                             monitor="dispatcher-alive", error="down")
