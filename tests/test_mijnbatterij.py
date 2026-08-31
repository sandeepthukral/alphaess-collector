"""Tests for the mijnbatterij.nl submitter.

Every number here goes onto a public leaderboard under this household's name and
cannot be retracted once posted, so the interesting cases are the ones where a
wrong value still looks plausible: an inverted power sign, a lifetime cycle count
that is really "since we started measuring", and a stale sample published as live.
"""

import datetime as dt

import pytest

import mijnbatterij as mb
import pricing
from conftest import constant_samples, hourly_intervals
from pricing import Sample

UTC = dt.UTC


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records what was sent instead of sending it."""

    def __init__(self, response=None, get_response=None):
        self.response = response or FakeResponse()
        self.get_response = get_response or FakeResponse(payload={})
        self.posts = []
        self.gets = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return self.response

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers})
        return self.get_response


def config(**overrides) -> dict:
    base = {
        "interval_s": 300,
        "timeout_s": 15,
        "stale_after_s": 600,
        "totals_ttl_s": 3600,
        "rank_interval_s": 0,
        "capacity_kwh": 27.9,
        "max_gap_s": 90.0,
        "cycles_offset": 0.0,
        "mode": "self_consumption",
        "load_balancing": False,
        "charge_positive": True,
        "base_url": mb.API_BASE,
    }
    base.update(overrides)
    return base


def sample(t: dt.datetime, *, battery: float = 0.0, soc: float = 50.0) -> Sample:
    return Sample(time=t, pv=0.0, grid=-battery, load=0.0, battery=battery, soc=soc)


# --------------------------------------------------------------------------
# today_window
# --------------------------------------------------------------------------

def test_the_window_opens_at_the_local_midnight_not_utc():
    """In CEST, 00:30 local on the 17th is 22:30 UTC on the 16th. A UTC-midnight
    window would report the last two hours of yesterday's charging as today's."""
    now = dt.datetime(2026, 7, 16, 22, 30, tzinfo=UTC)
    start, end = mb.today_window(now)
    assert start == dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    assert end == now


def test_the_window_ends_at_now():
    now = dt.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    start, end = mb.today_window(now)
    assert start == dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    assert end == now


# --------------------------------------------------------------------------
# battery_energy_kwh
# --------------------------------------------------------------------------

def test_a_steady_charge_is_all_charge_and_no_discharge():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = constant_samples(t0, t0 + dt.timedelta(hours=1), grid=1000.0, battery=-1000.0)
    charged, discharged, skipped = mb.battery_energy_kwh(samples)
    assert charged == pytest.approx(1.0, abs=1e-6)
    assert discharged == 0.0
    assert skipped == 0.0


def test_a_steady_discharge_is_all_discharge():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = constant_samples(t0, t0 + dt.timedelta(hours=2), grid=0.0, battery=500.0)
    charged, discharged, _ = mb.battery_energy_kwh(samples)
    assert charged == 0.0
    assert discharged == pytest.approx(1.0, abs=1e-6)


def test_an_interval_that_crosses_zero_counts_towards_both_totals():
    """The reason this reuses pricing._accumulate rather than a plain trapezoid:
    a pair that flips from charging to discharging must not net to a single
    smaller number, or a day of hard cycling reads as a quiet one."""
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = [sample(t0, battery=-1000.0), sample(t0 + dt.timedelta(hours=1), battery=1000.0)]
    charged, discharged, _ = mb.battery_energy_kwh(samples, max_gap_s=7200)
    assert charged == pytest.approx(0.25, abs=1e-6)
    assert discharged == pytest.approx(0.25, abs=1e-6)


def test_a_single_sample_integrates_to_nothing():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    assert mb.battery_energy_kwh([sample(t0, battery=-1000.0)]) == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# cycles
# --------------------------------------------------------------------------

def test_cycles_are_throughput_over_capacity():
    assert mb.cycles(27.9, 27.9) == pytest.approx(1.0)
    assert mb.cycles(13.95, 27.9) == pytest.approx(0.5)


def test_the_offset_carries_the_cycles_that_predate_the_collector():
    assert mb.cycles(27.9, 27.9, offset=120.0) == pytest.approx(121.0)


def test_an_unset_capacity_reports_the_offset_rather_than_dividing_by_zero():
    assert mb.cycles(500.0, 0.0, offset=7.0) == 7.0


# --------------------------------------------------------------------------
# build_payload
# --------------------------------------------------------------------------

def _payload(**overrides):
    args = {
        "latest": sample(dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC), battery=-1500.0, soc=62.4),
        "charged_kwh": 4.2345,
        "discharged_kwh": 1.0,
        "result_today": 1.23456,
        "result_total": 98.7654,
        "cycle_count": 121.345,
        "mode": "self_consumption",
        "load_balancing": False,
    }
    args.update(overrides)
    return mb.build_payload(**args)


def test_the_payload_carries_exactly_the_documented_fields():
    assert set(_payload()) == {
        "timestamp", "batteryResult", "batteryResultTotal", "batteryCharge",
        "batteryPower", "chargedToday", "dischargedToday", "totalBatteryCycles",
        "mode", "loadBalancingActive",
    }


def test_charging_is_sent_positive_by_default():
    """AlphaESS's pbat is positive while DISCHARGING, so -1500 W is charging at
    1.5 kW. Getting this backwards publishes a battery that appears to discharge
    all night, and nothing downstream can detect it."""
    assert _payload()["batteryPower"] == 1500


def test_the_sign_convention_is_a_setting_not_a_constant():
    assert _payload(charge_positive=False)["batteryPower"] == -1500


def test_the_timestamp_is_the_sample_time_not_the_submission_time():
    assert _payload()["timestamp"].startswith("2026-07-17T10:00:00")


def test_the_timestamp_is_offset_aware():
    assert _payload()["timestamp"].endswith("+00:00")


def test_euros_and_kwh_keep_enough_digits_to_be_summed():
    p = _payload()
    assert p["batteryResult"] == 1.2346
    assert p["chargedToday"] == 4.234
    assert p["batteryCharge"] == 62.4


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------

class FakeQueryApi:
    def query(self, flux):  # pragma: no cover - collect() patches the loaders
        return []


def _stub_influx(monkeypatch, samples, intervals, saving=10.0, discharge=279.0):
    monkeypatch.setattr(pricing, "load_samples_influx",
                        lambda *a, **k: samples)
    monkeypatch.setattr(pricing, "load_prices_influx",
                        lambda *a, **k: intervals)
    monkeypatch.setattr(mb, "stored_saving_total", lambda *a, **k: saving)
    monkeypatch.setattr(mb, "stored_discharge_total", lambda *a, **k: discharge)


def test_collect_builds_a_payload_from_todays_samples(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0, soc=55.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24))

    snap, outcome = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                               totals=mb.Totals(3600), config=config(), now=now)

    assert outcome == "ok"
    assert snap.payload["chargedToday"] == pytest.approx(12.0, abs=0.01)
    assert snap.payload["dischargedToday"] == 0.0
    assert snap.payload["batteryCharge"] == 55.0


def test_todays_result_is_added_to_the_stored_all_time_total(monkeypatch):
    """batteryResultTotal must include today, because daily_cost cannot: the
    nightly job has not run yet. Summing only stored rows would show the
    lifetime figure standing still all day and jumping at 02:00."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24), saving=10.0)

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(), now=now)

    assert snap.payload["batteryResultTotal"] == pytest.approx(
        10.0 + snap.payload["batteryResult"], abs=1e-4)


def test_todays_throughput_counts_towards_the_cycle_total(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=0.0, battery=1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24), discharge=27.9)

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(cycles_offset=100.0), now=now)

    # 27.9 kWh stored + 12 kWh today, over 27.9 kWh usable, plus the offset.
    assert snap.payload["totalBatteryCycles"] == pytest.approx(101.43, abs=0.01)


def test_a_stale_newest_sample_submits_nothing(monkeypatch):
    """A collector outage must not be published as a live reading -- the platform
    ranks installations against each other, so it would charge this one for it."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now - dt.timedelta(hours=2), grid=0.0, battery=0.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24))

    assert mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now) == (None, "stale")


def test_no_samples_at_all_submits_nothing(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    _stub_influx(monkeypatch, [], [])
    assert mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now) == (None, "no-data")


def test_an_empty_bucket_and_a_dead_collector_are_told_apart(monkeypatch):
    """They get fixed in different places -- one is a wrong sys_sn or an
    unscoped token, the other is an outage -- so a panel that shows only
    "nothing submitted" cannot tell you which of the two you have."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)

    _stub_influx(monkeypatch, [], [])
    _, absent = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                           totals=mb.Totals(3600), config=config(), now=now)

    old = constant_samples(start, now - dt.timedelta(hours=2), grid=0.0, battery=0.0)
    _stub_influx(monkeypatch, old, hourly_intervals(start, 24))
    _, stale = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                          totals=mb.Totals(3600), config=config(), now=now)

    assert (absent, stale) == ("no-data", "stale")


def test_missing_prices_send_a_zero_result_rather_than_no_submission(monkeypatch):
    """The price feed and the power feed fail independently. Without prices the
    euro figure is unknowable, but SoC, power and kWh are all still true."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, [])

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(), now=now)

    assert snap.payload["batteryResult"] == 0.0
    assert snap.payload["chargedToday"] == pytest.approx(12.0, abs=0.01)


# --------------------------------------------------------------------------
# Totals caching
# --------------------------------------------------------------------------

def test_the_all_time_totals_are_not_requeried_every_cycle(monkeypatch):
    """They change once a night; at a 300 s cadence an uncached sum is twelve
    full-history queries an hour for one new row a day."""
    calls = []
    monkeypatch.setattr(mb, "stored_saving_total",
                        lambda *a, **k: calls.append("saving") or 1.0)
    monkeypatch.setattr(mb, "stored_discharge_total",
                        lambda *a, **k: calls.append("discharge") or 2.0)
    totals = mb.Totals(3600)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)

    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1000.0)
    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1300.0)

    assert calls == ["saving", "discharge"]


def test_the_totals_are_refreshed_once_the_ttl_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(mb, "stored_saving_total",
                        lambda *a, **k: calls.append("saving") or 1.0)
    monkeypatch.setattr(mb, "stored_discharge_total",
                        lambda *a, **k: calls.append("discharge") or 2.0)
    totals = mb.Totals(3600)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)

    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1000.0)
    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1000.0 + 3600)

    assert calls == ["saving", "discharge", "saving", "discharge"]


# --------------------------------------------------------------------------
# submit / fetch_rank
# --------------------------------------------------------------------------

def test_a_successful_submission_sends_the_bearer_token_and_the_payload():
    session = FakeSession(FakeResponse(200))
    status = mb.submit(session, "key-123", {"batteryCharge": 50})
    assert status == 200
    sent = session.posts[0]
    assert sent["url"] == "https://api.mijnbatterij.nl/api/live"
    assert sent["headers"]["Authorization"] == "Bearer key-123"
    assert sent["json"] == {"batteryCharge": 50}


def test_a_rejection_carries_the_platforms_own_validation_message():
    """There is no published field-by-field reference for this API, so the body
    of a 400 is the only description of the schema that exists."""
    session = FakeSession(FakeResponse(400, text='{"error":"mode is invalid"}'))
    with pytest.raises(mb.SubmitError) as exc:
        mb.submit(session, "key", {})
    assert exc.value.status == 400
    assert "mode is invalid" in str(exc.value)


def test_an_auth_failure_names_the_setting_to_check():
    session = FakeSession(FakeResponse(401, text="unauthorized"))
    with pytest.raises(mb.SubmitError, match="MIJNBATTERIJ_API_KEY"):
        mb.submit(session, "stale-key", {})


def test_a_transport_failure_is_a_submit_error_too():
    """So the loop's backoff and heartbeat handle a DNS blip and a 500 the same
    way, rather than one of them escaping as an unhandled exception."""
    import requests

    class Broken(FakeSession):
        def post(self, *a, **k):
            raise requests.ConnectionError("name resolution failed")

    with pytest.raises(mb.SubmitError, match="request failed"):
        mb.submit(Broken(), "key", {})


def test_fetch_rank_reads_both_ranks():
    session = FakeSession(get_response=FakeResponse(
        payload={"resultToday": {"overallRank": 12, "providerRank": 3}}))
    assert mb.fetch_rank(session, "key") == {"overall_rank": 12.0, "provider_rank": 3.0}


def test_fetch_rank_tolerates_a_response_without_ranks():
    """A brand-new installation is not ranked yet, which is not an error."""
    session = FakeSession(get_response=FakeResponse(payload={"resultToday": {}}))
    assert mb.fetch_rank(session, "key") == {}


# --------------------------------------------------------------------------
# Status point
# --------------------------------------------------------------------------

def test_the_status_point_records_what_was_actually_sent():
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    snap = mb.Snapshot(_payload(), sample(now), age_s=12.0, sample_count=1200)
    line = mb.status_point(snap, "SN", "ok", 200, now).to_line_protocol()
    assert "mijnbatterij_submit" in line
    assert "outcome=ok" in line
    assert "submitted=1" in line
    assert "battery_result=1.2346" in line


def test_a_rejection_is_stored_as_a_point_too():
    """Otherwise the only record of a failed submission is a log line that
    rotates, and the dashboard shows a gap indistinguishable from a quiet night."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    line = mb.status_point(None, "SN", "rejected", 422, now).to_line_protocol()
    assert "submitted=0" in line
    assert "status_code=422" in line


# --------------------------------------------------------------------------
# Gap handling
# --------------------------------------------------------------------------

def test_an_outage_is_skipped_rather_than_trapezoided_across():
    """The trapezoid assumes the power ramped between two samples, which is true
    across 30 s and a fabrication across six hours. Charging at 4 kW either side
    of an outage would otherwise invent ~24 kWh of chargedToday, and unlike
    compute_day there is no gate() downstream to throw the day out afterwards."""
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = [sample(t0, battery=-4000.0),
               sample(t0 + dt.timedelta(hours=6), battery=-4000.0)]
    charged, discharged, skipped = mb.battery_energy_kwh(samples, max_gap_s=90)
    assert (charged, discharged) == (0.0, 0.0)
    assert skipped == 6 * 3600


def test_normal_cadence_drift_is_still_integrated():
    """The cap must not fire on a poll or two being late, or every figure is
    quietly low all the time."""
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = [sample(t0, battery=-3600.0), sample(t0 + dt.timedelta(seconds=60),
                                                   battery=-3600.0)]
    charged, _, skipped = mb.battery_energy_kwh(samples, max_gap_s=90)
    assert charged == pytest.approx(0.06, abs=1e-6)
    assert skipped == 0.0


def test_a_gap_does_not_discard_the_energy_either_side_of_it():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = [
        sample(t0, battery=-1000.0),
        sample(t0 + dt.timedelta(hours=1), battery=-1000.0),          # 1 kWh
        sample(t0 + dt.timedelta(hours=7), battery=-1000.0),          # 6 h gap
        sample(t0 + dt.timedelta(hours=8), battery=-1000.0),          # 1 kWh
    ]
    charged, _, skipped = mb.battery_energy_kwh(samples, max_gap_s=3600)
    assert charged == pytest.approx(2.0, abs=1e-6)
    assert skipped == 6 * 3600


def test_the_skipped_seconds_reach_the_status_point(monkeypatch):
    """So a panel can say the figure is under-reported instead of leaving it to
    be inferred from a hole in power_readings."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    snap = mb.Snapshot(_payload(), sample(now), age_s=1.0, sample_count=2, skipped_s=21600.0)
    assert "gap_skipped_s=21600" in mb.status_point(snap, "SN", "ok", 200, now).to_line_protocol()


# --------------------------------------------------------------------------
# The nightly cycle-count hole
# --------------------------------------------------------------------------

class RecordingQueryApi:
    """Returns a fixed sum, and remembers every flux it was handed."""

    def __init__(self, value=100.0, rows=True):
        self.value = value
        self.rows = rows
        self.queries = []

    def query(self, flux):
        self.queries.append(flux)

        class Rec:
            @staticmethod
            def get_value():
                return None

        class Table:
            records = [Rec()] if self.rows else []

        return [Table()]


def test_yesterday_is_filled_from_power_readings_until_the_nightly_job_runs(monkeypatch):
    """daily_energy lands at ~03:00. Between midnight and then, yesterday is in
    no stored row while today's own discharge has just reset to zero -- so a
    naive sum drops totalBatteryCycles by about a full cycle every night. A
    lifetime counter moving backwards is physically impossible and reads
    downstream as corrupt data, not as a late batch job."""
    monkeypatch.setattr(mb, "_sum_query", lambda *a, **k: 100.0)
    monkeypatch.setattr(mb, "has_daily_energy", lambda *a, **k: False)
    monkeypatch.setattr(mb, "discharge_from_readings", lambda *a, **k: 18.5)

    now = dt.datetime(2026, 7, 17, 0, 30, tzinfo=UTC)
    total = mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                                      dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC), now=now)
    assert total == pytest.approx(118.5)


def test_nothing_is_filled_in_once_the_row_exists(monkeypatch):
    """Otherwise the fix double-counts yesterday from 03:00 onwards, which is a
    worse error than the dip it replaces."""
    monkeypatch.setattr(mb, "_sum_query", lambda *a, **k: 100.0)
    monkeypatch.setattr(mb, "has_daily_energy", lambda *a, **k: True)
    monkeypatch.setattr(mb, "discharge_from_readings",
                        lambda *a, **k: pytest.fail("should not have been called"))

    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    total = mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                                      dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC), now=now)
    assert total == pytest.approx(100.0)


def test_the_fill_looks_at_yesterday_in_local_time():
    """At 00:30 CEST on the 17th -- 22:30 UTC on the 16th -- yesterday is the
    16th, not the 15th."""
    api = RecordingQueryApi(rows=True)
    mb.has_daily_energy(api, "alphaess", "SN", dt.date(2026, 7, 16))
    assert "2026-07-15T22:00:00+00:00" in api.queries[0]


# --------------------------------------------------------------------------
# run_once: what a rejection records
# --------------------------------------------------------------------------

class RecordingWriteApi:
    def __init__(self):
        self.points = []

    def write(self, bucket=None, record=None):
        self.points.append(record.to_line_protocol())


def _run_once(monkeypatch, response, samples=None, **cfg):
    now_start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    if samples is None:
        samples = constant_samples(now_start, dt.datetime.now(UTC), grid=1000.0,
                                   battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(now_start, 48))
    write_api = RecordingWriteApi()
    session = FakeSession(response)
    return write_api, session, mb.run_once(
        FakeQueryApi(), write_api, session, bucket="alphaess", sys_sn="SN",
        api_key="key", config=config(**cfg), totals=mb.Totals(3600), dry_run=False)


def test_a_rejected_submission_records_what_was_in_the_body(monkeypatch):
    """The whole point of mijnbatterij_submit is being able to debug a 422 after
    the fact. Recording only `submitted=0` and the status code leaves you
    guessing what was actually sent."""
    write_api = RecordingWriteApi()
    session = FakeSession(FakeResponse(422, text="dischargedToday out of range"))
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, dt.datetime.now(UTC), grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 48))

    with pytest.raises(mb.SubmitError):
        mb.run_once(FakeQueryApi(), write_api, session, bucket="alphaess", sys_sn="SN",
                    api_key="key", config=config(), totals=mb.Totals(3600), dry_run=False)

    assert len(write_api.points) == 1
    point = write_api.points[0]
    assert "outcome=rejected" in point
    assert "status_code=422" in point
    assert "discharged_today=" in point
    assert "battery_result=" in point


def test_an_unreachable_platform_is_labelled_apart_from_a_rejection(monkeypatch):
    """A 4xx will fail identically next time and needs a code or config change;
    a transport failure is worth simply trying again in five minutes."""
    import requests

    class Broken(FakeSession):
        def post(self, *a, **k):
            raise requests.ConnectionError("name resolution failed")

    write_api = RecordingWriteApi()
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, dt.datetime.now(UTC), grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 48))

    with pytest.raises(mb.SubmitError):
        mb.run_once(FakeQueryApi(), write_api, session=Broken(), bucket="alphaess",
                    sys_sn="SN", api_key="key", config=config(),
                    totals=mb.Totals(3600), dry_run=False)

    assert "outcome=unreachable" in write_api.points[0]


def test_a_declined_cycle_records_the_reason_it_declined(monkeypatch):
    now_utc = dt.datetime.now(UTC)
    start = dt.datetime.combine(now_utc.date(), dt.time(), UTC) - dt.timedelta(days=1)
    stale = constant_samples(start, now_utc - dt.timedelta(hours=3), grid=0.0, battery=0.0)
    write_api, _, (snap, outcome) = _run_once(monkeypatch, FakeResponse(200), samples=stale)
    assert (snap, outcome) == (None, "stale")
    assert "outcome=stale" in write_api.points[0]
    assert "submitted=0" in write_api.points[0]


# --------------------------------------------------------------------------
# fetch_rank: an undocumented API cannot be allowed to kill the loop
# --------------------------------------------------------------------------

def test_a_rank_body_of_the_wrong_shape_is_not_a_crash():
    """A list where an object was expected raises AttributeError, not
    SubmitError -- and out in run_loop that ends the process, which
    `restart: unless-stopped` then turns into a crash loop that stops every
    submission over a decoration on a Grafana panel."""
    session = FakeSession(get_response=FakeResponse(payload=[{"overallRank": 1}]))
    assert mb.fetch_rank(session, "key") == {}


def test_a_null_result_today_is_not_a_crash():
    session = FakeSession(get_response=FakeResponse(payload={"resultToday": None}))
    assert mb.fetch_rank(session, "key") == {}


def test_a_non_numeric_rank_is_dropped_rather_than_raising():
    session = FakeSession(get_response=FakeResponse(
        payload={"resultToday": {"overallRank": "n/a", "providerRank": 4}}))
    assert mb.fetch_rank(session, "key") == {"provider_rank": 4.0}
