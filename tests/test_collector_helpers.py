"""Tests for the pure helpers in collector.py.

These feed the log lines and the phone notification, which is the only view of
an outage available while it is happening.
"""

import pytest

from collector import (
    error_summary,
    format_duration,
    parse_fields,
    recovery_message,
)

# --------------------------------------------------------------------------
# error_summary
# --------------------------------------------------------------------------

def test_error_summary_keeps_the_innermost_cause():
    """requests wraps urllib3 wraps ssl; the fault is the trailing cause.

    A head-truncation would keep 'HTTPSConnectionPool(host=...' and throw away
    the only part that names what broke.
    """
    exc = ConnectionError(
        "HTTPSConnectionPool(host='openapi.alphaess.com', port=443): Max "
        "retries exceeded with url: /api/getLastPowerData (Caused by "
        "SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol')))")
    summary = error_summary(exc)
    assert summary.startswith("ConnectionError: SSLError")
    assert "HTTPSConnectionPool" not in summary


def test_error_summary_collapses_whitespace():
    exc = RuntimeError("line one\n    line two\tline three")
    assert error_summary(exc) == "RuntimeError: line one line two line three"


def test_error_summary_truncates_long_messages():
    from collector import MAX_ERROR_SUMMARY_CHARS
    exc = RuntimeError("x" * 500)
    summary = error_summary(exc)
    assert summary.endswith("...")
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS + len("RuntimeError: ") + 3


def test_error_summary_without_a_message():
    assert error_summary(ValueError()) == "ValueError"


def test_error_summary_redacts_the_query_string():
    """HTTPError puts the request URL straight in the message, sysSn and all.

    Unlike a connection error there's no "(Caused by ...)" segment to keep it
    out of the summary that gets pushed to Uptime Kuma.
    """
    exc = Exception(
        "401 Client Error: Unauthorized for url: "
        "https://openapi.alphaess.com/api/getLastPowerData?sysSn=AL5006148000012345")
    summary = error_summary(exc)
    assert "sysSn" not in summary
    assert "AL5006148000012345" not in summary
    assert summary.endswith(
        "https://openapi.alphaess.com/api/getLastPowerData")


# --------------------------------------------------------------------------
# format_duration / recovery_message
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (9, "9s"), (59, "59s"), (60, "1m00s"),
    (61, "1m01s"), (3600, "60m00s"), (905, "15m05s"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_recovery_message_on_a_healthy_poll():
    assert recovery_message(0, 0.0) == "OK"


def test_recovery_message_after_an_outage():
    msg = recovery_message(4, 905)
    assert "4 failures" in msg
    assert "15m05s" in msg


def test_recovery_message_carries_the_cause():
    msg = recovery_message(2, 180, "fetch: ConnectionError: EAI_AGAIN [upstream]")
    assert "2 failures" in msg
    assert "3m00s" in msg
    assert "fetch: ConnectionError: EAI_AGAIN [upstream]" in msg


def test_recovery_message_omits_an_empty_cause():
    """No dangling separator when the outage healed before anything diagnosed
    it -- the message still has to read as a sentence on a phone."""
    assert recovery_message(2, 180) == "OK (recovered after 2 failures, 3m00s)"


def test_a_cause_on_a_healthy_poll_is_ignored():
    assert recovery_message(0, 0.0, "fetch: ConnectionError") == "OK"


# --------------------------------------------------------------------------
# parse_fields
# --------------------------------------------------------------------------

def test_parse_fields_maps_the_api_names():
    fields = parse_fields({"ppv": 1500, "pgrid": -200, "pload": 800,
                           "pbat": -500, "soc": 87.5})
    assert fields == {
        "pv_power_w": 1500.0, "grid_power_w": -200.0, "load_power_w": 800.0,
        "battery_power_w": -500.0, "soc_percent": 87.5,
    }
    assert all(isinstance(v, float) for v in fields.values())


def test_parse_fields_coerces_numeric_strings():
    """The API has been seen returning numbers as JSON strings."""
    assert parse_fields({"ppv": "1500.5"})["pv_power_w"] == 1500.5


def test_parse_fields_omits_missing_keys_rather_than_writing_null(caplog):
    fields = parse_fields({"ppv": 1500, "soc": 50})
    assert set(fields) == {"pv_power_w", "soc_percent"}
    assert "missing fields" in caplog.text


def test_parse_fields_keeps_zero_values():
    """0 W is a real reading; a falsy-vs-None mixup would drop it."""
    fields = parse_fields({"ppv": 0, "pgrid": 0, "pload": 0, "pbat": 0, "soc": 0})
    assert len(fields) == 5
    assert all(v == 0.0 for v in fields.values())


def test_parse_fields_on_an_empty_response():
    assert parse_fields({}) == {}


def test_parse_fields_overrides_grid_and_recomputes_load_from_p1():
    fields = parse_fields(
        {"ppv": 1500, "pgrid": -9999, "pload": -9999, "pbat": -500, "soc": 87.5},
        p1_data={"active_power_w": 200},
    )
    assert fields["grid_power_w"] == 200.0
    # load = pv + grid + battery = 1500 + 200 + (-500)
    assert fields["load_power_w"] == 1200.0
    assert fields["pv_power_w"] == 1500.0
    assert fields["battery_power_w"] == -500.0


def test_parse_fields_ignores_p1_data_when_none():
    fields = parse_fields({"ppv": 1500, "pgrid": -200, "pload": 800,
                           "pbat": -500, "soc": 87.5}, p1_data=None)
    assert fields["grid_power_w"] == -200.0
    assert fields["load_power_w"] == 800.0


def test_parse_fields_stays_empty_on_an_all_none_response_even_with_p1_data():
    """A degraded/empty AlphaESS poll (every field None) must produce {}, not a point
    holding only the P1 reading -- run_loop()'s `if fields:` guard exists specifically to
    skip writing a point for a poll like this, and an unconditional P1 injection would make
    an otherwise-empty poll look non-empty, silently changing what "nothing to write" means."""
    fields = parse_fields(
        {"ppv": None, "pgrid": None, "pload": None, "pbat": None, "soc": None},
        p1_data={"active_power_w": 300},
    )
    assert fields == {}


# --------------------------------------------------------------------------
# fetch_p1_data
# --------------------------------------------------------------------------

def test_fetch_p1_data_returns_the_body(monkeypatch):
    import collector as collector_mod

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"active_power_w": 8742, "active_power_l1_w": 3640}

    monkeypatch.setattr(collector_mod.requests, "get",
                        lambda url, timeout=10: FakeResponse())
    body = collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data")
    assert body["active_power_w"] == 8742


def test_fetch_p1_data_raises_on_missing_active_power_w(monkeypatch):
    import collector as collector_mod

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"active_power_l1_w": 3640}

    monkeypatch.setattr(collector_mod.requests, "get",
                        lambda url, timeout=10: FakeResponse())
    with pytest.raises(RuntimeError, match="active_power_w"):
        collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data")


def test_fetch_p1_data_wraps_connection_error_as_runtime_error(monkeypatch):
    import collector as collector_mod

    def fake_get(*args, **kwargs):
        raise collector_mod.requests.exceptions.ConnectionError("Network unreachable")

    monkeypatch.setattr(collector_mod.requests, "get", fake_get)
    with pytest.raises(RuntimeError, match="P1 fetch failed"):
        collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data")


def test_fetch_p1_data_forwards_custom_timeout(monkeypatch):
    import collector as collector_mod

    captured_kwargs = {}

    def fake_get(url, timeout=10):
        captured_kwargs["timeout"] = timeout
        class FakeResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return {"active_power_w": 100}
        return FakeResponse()

    monkeypatch.setattr(collector_mod.requests, "get", fake_get)
    collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data", timeout=25)
    assert captured_kwargs["timeout"] == 25


def test_parse_fields_with_p1_data_but_missing_battery():
    """When p1_data overrides grid but battery is missing, load_power_w can't be recomputed
    from the identity -- it must be dropped, not left holding AlphaESS's own stale (and
    known-wrong, single-phase) `pload` figure."""
    fields = parse_fields(
        {"ppv": 1500, "pgrid": -9999, "pload": 1800, "soc": 87.5},
        p1_data={"active_power_w": 200},
    )
    assert fields["grid_power_w"] == 200.0
    assert "load_power_w" not in fields  # dropped, not recomputed and not left stale
    assert fields["pv_power_w"] == 1500.0
    assert "battery_power_w" not in fields


# --------------------------------------------------------------------------
# run_once
# --------------------------------------------------------------------------

def test_run_once_fetches_and_prints_p1_data_under_grid_source_p1(monkeypatch, capsys):
    """--once under GRID_SOURCE=p1 must show the P1-corrected values the running poll loop
    will actually record, not AlphaESS's own known-wrong reading -- otherwise the "verify
    before trusting dashboards" check parse_fields's docstring describes is a false
    confidence check for the one field this feature exists to fix."""
    import collector as collector_mod

    monkeypatch.setenv("GRID_SOURCE", "p1")
    monkeypatch.setenv("P1_MONITOR_URL", "http://192.168.2.46/api/v1/data")
    monkeypatch.setattr(
        collector_mod, "get_last_power_data",
        lambda *a, **k: {"ppv": 1000, "pgrid": -9999, "pload": -9999,
                         "pbat": -200, "soc": 80})
    monkeypatch.setattr(collector_mod, "fetch_p1_data",
                        lambda url, timeout=10: {"active_power_w": 300})
    collector_mod.run_once("id", "secret", "SN")
    out = capsys.readouterr().out
    assert "Raw P1 monitor data object" in out
    assert '"active_power_w": 300' in out
    assert '"grid_power_w": 300.0' in out
    assert '"load_power_w": 1100.0' in out  # 1000 + 300 + (-200)


def test_run_once_skips_p1_fetch_under_default_grid_source(monkeypatch, capsys):
    import collector as collector_mod

    called = []
    monkeypatch.setattr(
        collector_mod, "get_last_power_data",
        lambda *a, **k: {"ppv": 1000, "pgrid": -100, "pload": 900,
                         "pbat": -200, "soc": 80})
    monkeypatch.setattr(collector_mod, "fetch_p1_data",
                        lambda url, timeout=10: called.append(1))
    collector_mod.run_once("id", "secret", "SN")
    assert called == []
    assert "Raw P1 monitor data object" not in capsys.readouterr().out


def test_run_once_exits_when_grid_source_p1_missing_url(monkeypatch):
    import collector as collector_mod

    monkeypatch.setenv("GRID_SOURCE", "p1")
    monkeypatch.delenv("P1_MONITOR_URL", raising=False)
    with pytest.raises(SystemExit):
        collector_mod.run_once("id", "secret", "SN")
