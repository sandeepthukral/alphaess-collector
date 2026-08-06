"""The nightly job's dead-man's switch, and the failure it exists for.

A run where every day fails the quality gate exits 0, logs warnings nobody
reads, and writes nothing. To DSM Task Scheduler, to the container, and to any
check watching the exit code, that is a healthy night -- and the dashboard just
quietly stops moving. The heartbeat is pushed from the job after a row actually
lands, and pushed *down* with the reason when nothing does, which is the only
signal that separates those two cases.
"""

import datetime as dt

import efficiency
from test_efficiency_write import (  # noqa: F401  (harness pulls in its fixtures)
    DAY,
    ENERGY,
    STEPS,
    FakeInfluxClient,
    FakeWriteApi,
    harness,
    metered_records,
    run,
)


def summary_with(**kwargs) -> efficiency.RunSummary:
    summary = efficiency.RunSummary()
    for key, value in kwargs.items():
        setattr(summary, key, value)
    return summary


# --------------------------------------------------------------------------
# The message, in isolation
# --------------------------------------------------------------------------

def test_a_written_day_reports_the_number_it_wrote():
    """"OK" alone costs a trip to the dashboard to find out whether the figure
    is plausible. The alert should answer that from a phone."""
    summary = summary_with(
        written=[DAY], skipped=[DAY - dt.timedelta(days=1)],
        last_result={"conversion_loss_kwh": 1.39, "battery_loss_kwh": 0.12,
                     "total_loss_kwh": 1.51})
    status, msg = summary.heartbeat()
    assert status == "up"
    assert "2026-08-05" in msg and "1.51" in msg and "1.39" in msg
    assert "1 written" in msg and "1 skipped" in msg


def test_a_night_with_nothing_new_to_do_is_healthy_not_silent():
    """Without this the monitor flaps every time the rolling window is already
    full -- and an alert that cries wolf nightly is one nobody reads."""
    status, msg = summary_with(skipped=[DAY]).heartbeat()
    assert status == "up"
    assert "nothing new" in msg and "2026-08-05" in msg


def test_a_fully_gated_run_pushes_down_with_the_reason():
    """The case this whole mechanism exists for: exit code 0, no rows."""
    status, msg = summary_with(gated=[(DAY, "soc_align 4.2pp > 2.0")]).heartbeat()
    assert status == "down"
    assert "GATED" in msg and "soc_align" in msg


def test_a_throttled_run_names_the_rate_limit():
    status, msg = summary_with(throttled=[DAY]).heartbeat()
    assert status == "down"
    assert "THROTTLED" in msg


def test_a_failed_run_names_the_error():
    status, msg = summary_with(failed=[(DAY, "code 6002 (invalid sign)")]).heartbeat()
    assert status == "down"
    assert "6002" in msg


def test_an_empty_upstream_response_pushes_down():
    """AlphaESS returning nothing is not the same as nothing being wrong."""
    status, msg = summary_with(empty=[DAY]).heartbeat()
    assert status == "down"
    assert "NO DATA" in msg


def test_a_write_outranks_a_gate_in_the_same_run():
    """A 4-day window where three days were already done and the fourth wrote
    is a good night, even if a fifth was gated."""
    summary = summary_with(
        written=[DAY], gated=[(DAY - dt.timedelta(days=1), "thin")],
        last_result={"conversion_loss_kwh": 1.0})
    assert summary.heartbeat()[0] == "up"


def test_a_run_that_did_nothing_at_all_pushes_nothing():
    """No days requested is not a health signal in either direction."""
    assert efficiency.RunSummary().heartbeat() is None


# --------------------------------------------------------------------------
# Wired through main()
# --------------------------------------------------------------------------

def _main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["efficiency.py", *argv])
    efficiency.main()


def test_main_pushes_up_after_a_real_write(harness, monkeypatch):  # noqa: F811
    _main(monkeypatch, "--date", DAY.isoformat())
    assert [s for s, _ in harness["beats"]] == ["up"]


def test_main_pushes_down_when_every_day_is_gated(harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(efficiency, "MAX_SOC_ALIGN_PP", 0.0001)
    harness["records"] = metered_records(DAY, [900.0] * STEPS, soc=99.0)
    _main(monkeypatch, "--date", DAY.isoformat())
    assert harness["beats"] and harness["beats"][0][0] == "down"
    assert "GATED" in harness["beats"][0][1]


def test_a_dry_run_never_pushes(harness, monkeypatch):  # noqa: F811
    """It computes nothing durable. Pushing "up" for a hand-run would let an
    operator poking at yesterday mask a broken nightly job for another day."""
    _main(monkeypatch, "--dry-run", "--date", DAY.isoformat())
    assert harness["beats"] == []


def test_nothing_is_pushed_when_no_url_is_configured(harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(efficiency, "HEARTBEAT_URL", "")
    _main(monkeypatch, "--date", DAY.isoformat())
    assert harness["beats"] == []


def test_a_broken_heartbeat_never_fails_the_run(harness, monkeypatch):  # noqa: F811
    """Monitoring is the least important thing this job does. collector.py's
    send_heartbeat already swallows its own errors; this pins that the job does
    not add a way around that."""
    def boom(*a, **k):
        raise RuntimeError("kuma is down")

    monkeypatch.setattr(efficiency, "send_heartbeat", boom)
    _main(monkeypatch, "--date", DAY.isoformat())
    assert len(harness["write_api"].points("daily_energy")) == 1
