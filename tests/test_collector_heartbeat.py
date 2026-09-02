"""`collector.send_heartbeat` -- the outbound half of the dead man's switch.

The function's own errors are swallowed, because monitoring must never be able to stop the
thing it monitors. What it must NOT do is swallow them silently: it returns a reason, and
the poll loop turns that into a `collector_health` point (see
`test_collector_failure_domains.py::TestAnUndeliverableHeartbeatIsVisible`).
"""
from __future__ import annotations

import pytest
import requests

import collector

URL = "http://kuma.local:3001/api/push/wmcUwPgJo4?status=up&msg=OK&ping="


class FakeResponse:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error for url: {URL}")


def capture(monkeypatch, response=None, raises=None):
    """Patch requests.get and record the URL it was called with."""
    seen: list[str] = []

    def fake_get(url, timeout=None):
        seen.append(url)
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(collector.requests, "get", fake_get)
    return seen


class TestTheReturnValue:
    def test_a_delivered_ping_reports_nothing(self, monkeypatch):
        capture(monkeypatch, response=FakeResponse(200))
        assert collector.send_heartbeat(URL, "up", "OK") == ""

    def test_an_unset_url_is_not_a_failure(self, monkeypatch):
        """An unconfigured heartbeat is a choice, not a fault. Reporting it would write a
        health event on every poll for every deployment that never opted in."""
        seen = capture(monkeypatch, response=FakeResponse(200))
        assert collector.send_heartbeat("", "up", "OK") == ""
        assert seen == [], "an empty URL must not reach the network"

    def test_a_transport_failure_reports_the_reason(self, monkeypatch):
        """The 2026-08-29 shape: the Kuma host moved and every ping timed out against the
        old address for hours, visible only as repeated log lines."""
        capture(monkeypatch, raises=requests.ConnectTimeout(
            "HTTPConnectionPool(host='192.168.68.105', port=3001): timed out"))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert reason.startswith("ConnectTimeout")

    def test_a_non_2xx_reply_counts_as_a_failure(self, monkeypatch):
        """A wrong or revoked push token answers 404 and the REQUEST succeeds. Without the
        status check that is the one error that disables a monitor forever while looking
        entirely healthy from this side."""
        capture(monkeypatch, response=FakeResponse(404))
        assert collector.send_heartbeat(URL, "up", "OK").startswith("HTTPError")


class TestTheReasonIsSafeToStore:
    def test_the_push_token_is_redacted(self, monkeypatch):
        """`_URL_QUERY_RE` strips query strings, but a Kuma push token lives in the PATH, and
        `requests` quotes the failing URL. Tolerable in a log; not once the same string is
        written to InfluxDB and rendered on a dashboard."""
        capture(monkeypatch, raises=requests.ConnectionError(
            "HTTPConnectionPool(host='kuma.local', port=3001): Max retries exceeded "
            "with url: /api/push/wmcUwPgJo4"))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert "wmcUwPgJo4" not in reason
        assert "/api/push/<token>" in reason

    def test_the_reason_is_bounded(self, monkeypatch):
        """It becomes an InfluxDB field value. `error_summary` already caps this; the point
        of pinning it here is that the cap survives being routed through the new path."""
        capture(monkeypatch, raises=requests.ConnectionError("x" * 5000))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert len(reason) <= collector.MAX_ERROR_SUMMARY_CHARS + 64


class TestItStillNeverRaises:
    @pytest.mark.parametrize("boom", [
        requests.ConnectTimeout("timed out"),
        requests.ConnectionError("refused"),
        ValueError("something unexpected entirely"),
    ])
    def test_no_exception_escapes(self, monkeypatch, boom):
        capture(monkeypatch, raises=boom)
        collector.send_heartbeat(URL, "down", "why")  # must not raise
