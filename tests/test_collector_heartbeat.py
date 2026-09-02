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
            # The real message shape, URL and all -- this is the branch that
            # actually carries the push token into the reason string.
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Not Found for url: {URL}")


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
            "HTTPConnectionPool(host='192.168.68.105', port=3001): Max retries exceeded "
            "with url: /api/push/wmcUwPgJo4 (Caused by ConnectTimeoutError("
            "'Connection to 192.168.68.105 timed out. (connect timeout=5)'))"))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert reason.startswith("ConnectTimeout")
        assert "timed out" in reason

    def test_a_non_2xx_reply_counts_as_a_failure(self, monkeypatch):
        """A wrong or revoked push token answers 404 and the REQUEST succeeds. Without the
        status check that is the one error that disables a monitor forever while looking
        entirely healthy from this side."""
        capture(monkeypatch, response=FakeResponse(404))
        assert collector.send_heartbeat(URL, "up", "OK").startswith("HTTPError")


class TestTheReasonIsSafeToStore:
    def test_the_push_token_is_redacted_from_a_404(self, monkeypatch):
        """THE 404 IS THE BRANCH THAT LEAKS, which is not the obvious one. `requests` puts
        the failing URL straight into an HTTPError's message, and `_URL_QUERY_RE` only strips
        the query string -- a Kuma push token lives in the PATH and survives it.

        Tolerable in a log; not once the same string is written to InfluxDB and rendered on a
        dashboard -- and on this deployment not in the log either, since Alloy ships it to
        Loki and Grafana renders that too.
        """
        capture(monkeypatch, response=FakeResponse(404))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert "wmcUwPgJo4" not in reason
        assert "/api/push/<token>" in reason

    def test_a_transport_error_never_reaches_the_redaction_at_all(self, monkeypatch):
        """Pinned so nobody mistakes the redaction above for what protects this path.

        A real connection error is always wrapped: `requests` renders it with a trailing
        "(Caused by ...)", and `error_summary` keeps ONLY that innermost cause -- which names
        the host and port and carries no URL. So the token cannot survive this branch even
        with the redaction removed, and a test asserting otherwise would be guarding a shape
        that never occurs.
        """
        capture(monkeypatch, raises=requests.ConnectionError(
            "HTTPConnectionPool(host='kuma.local', port=3001): Max retries exceeded with "
            "url: /api/push/wmcUwPgJo4 (Caused by NewConnectionError("
            "'Failed to establish a new connection: [Errno 113] No route to host'))"))
        reason = collector.send_heartbeat(URL, "up", "OK")
        assert "wmcUwPgJo4" not in reason
        assert "/api/push" not in reason, "the innermost cause carries no URL"
        assert "No route to host" in reason

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


class TestTheLogLineIsRedactedToo:
    def test_the_token_does_not_reach_the_log(self, monkeypatch, caplog):
        """The container log is not a private destination here: Alloy ships it to Loki and
        the NAS Grafana renders it, which is the same audience as the stored field. Scrubbing
        only the return value would move the leak rather than close it."""
        capture(monkeypatch, response=FakeResponse(404))
        with caplog.at_level("WARNING"):
            collector.send_heartbeat(URL, "up", "OK")
        assert caplog.records, "a failed push must still be logged"
        assert "wmcUwPgJo4" not in caplog.text
        assert "<token>" in caplog.text
