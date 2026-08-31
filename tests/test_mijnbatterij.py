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
    charged, discharged = mb.battery_energy_kwh(samples)
    assert charged == pytest.approx(1.0, abs=1e-6)
    assert discharged == 0.0


def test_a_steady_discharge_is_all_discharge():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = constant_samples(t0, t0 + dt.timedelta(hours=2), grid=0.0, battery=500.0)
    charged, discharged = mb.battery_energy_kwh(samples)
    assert charged == 0.0
    assert discharged == pytest.approx(1.0, abs=1e-6)


def test_an_interval_that_crosses_zero_counts_towards_both_totals():
    """The reason this reuses pricing._accumulate rather than a plain trapezoid:
    a pair that flips from charging to discharging must not net to a single
    smaller number, or a day of hard cycling reads as a quiet one."""
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    samples = [sample(t0, battery=-1000.0), sample(t0 + dt.timedelta(hours=1), battery=1000.0)]
    charged, discharged = mb.battery_energy_kwh(samples)
    assert charged == pytest.approx(0.25, abs=1e-6)
    assert discharged == pytest.approx(0.25, abs=1e-6)


def test_a_single_sample_integrates_to_nothing():
    t0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    assert mb.battery_energy_kwh([sample(t0, battery=-1000.0)]) == (0.0, 0.0)


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

    snap = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now)

    assert snap is not None
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

    snap = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now)

    assert snap.payload["batteryResultTotal"] == pytest.approx(
        10.0 + snap.payload["batteryResult"], abs=1e-4)


def test_todays_throughput_counts_towards_the_cycle_total(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=0.0, battery=1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24), discharge=27.9)

    snap = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
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
                      totals=mb.Totals(3600), config=config(), now=now) is None


def test_no_samples_at_all_submits_nothing(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    _stub_influx(monkeypatch, [], [])
    assert mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now) is None


def test_missing_prices_send_a_zero_result_rather_than_no_submission(monkeypatch):
    """The price feed and the power feed fail independently. Without prices the
    euro figure is unknowable, but SoC, power and kWh are all still true."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, [])

    snap = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
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
