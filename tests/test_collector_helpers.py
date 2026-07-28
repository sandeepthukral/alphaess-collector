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
