"""Tests for the mijnbatterij.nl submitter.

Every number here goes onto a public leaderboard under this household's name and
cannot be retracted once posted, so the interesting cases are the ones where a
wrong value still looks plausible: an inverted power sign, a lifetime cycle count
that is really "since we started measuring", and a stale sample published as live.
"""

import datetime as dt
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

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
        "test": False,
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
        "mode": "",
        "load_balancing": False,
    }
    args.update(overrides)
    return mb.build_payload(**args)


def test_the_payload_matches_the_published_schema():
    """Field names and presence, against the OpenAPI spec at
    https://onbalansmarkt.com/help/api-docs/ -- not against a third-party
    integration's example, which is where the original list came from and which
    carried a `batteryPower` field the API does not define."""
    assert set(_payload()) == {
        "timestamp", "batteryResult", "batteryResultTotal", "batterySavings",
        "batteryCharge", "chargedToday", "dischargedToday", "gridImportToday",
        "gridExportToday", "solarKwhGenerated", "totalBatteryCycles",
        "loadBalancingActive",
    }


def test_there_is_no_battery_power_field():
    """The spec defines none. The original payload sent one for weeks and the
    API silently ignored it, which is precisely why it went unnoticed."""
    assert "batteryPower" not in _payload()


def test_every_numeric_field_is_a_string():
    """The spec types them all as `string`. /api/live tolerates real JSON
    numbers, which is why this survived until /api/results/monthly refused a
    payload outright."""
    payload = _payload()
    for key in ("batteryResult", "batteryResultTotal", "batteryCharge",
                "chargedToday", "dischargedToday", "totalBatteryCycles"):
        assert isinstance(payload[key], str), key


def test_load_balancing_is_on_or_off_not_a_boolean():
    """`loadBalancingActive` is an enum of 'on' | 'off' in the spec."""
    assert _payload(load_balancing=False)["loadBalancingActive"] == "off"
    assert _payload(load_balancing=True)["loadBalancingActive"] == "on"


def test_an_empty_mode_is_omitted_rather_than_sent_blank():
    """The spec says mode OVERRIDES the profile page's own Modus, so sending
    nothing is how the profile stands. Which values a given account may use
    varies by provider: frank-energie rejected `self_consumption` outright."""
    assert "mode" not in _payload(mode="")
    assert _payload(mode="manual")["mode"] == "manual"


def test_the_result_is_also_sent_as_savings():
    """The spec defines batteryResult = batteryResultImbalance + batterySavings.
    Nothing here trades imbalance, so the two are the same number."""
    p = _payload()
    assert p["batteryResult"] == p["batterySavings"] == "1.23"


def test_a_shaky_day_is_flagged_invalid_rather_than_withheld():
    """The API has a field for exactly the days this service already detects --
    a gap it refused to integrate across, a price feed too thin to price the
    day. Publishing the number with a flag beats publishing it silently."""
    assert "invalid" not in _payload()
    assert _payload(invalid=True)["invalid"] is True


def test_the_test_flag_asks_the_api_to_validate_without_storing():
    assert "test" not in _payload()
    assert _payload(test=True)["test"] is True


def test_the_timestamp_is_the_sample_time_not_the_submission_time():
    assert _payload()["timestamp"].startswith("2026-07-17T10:00:00")


def test_the_timestamp_is_offset_aware():
    assert _payload()["timestamp"].endswith("+00:00")


def test_grid_and_solar_energy_are_carried():
    """Optional in the spec, and what enables its 15-minute household tracking."""
    p = _payload(imported_kwh=6.4, exported_kwh=3.1, solar_kwh=12.5)
    assert (p["gridImportToday"], p["gridExportToday"], p["solarKwhGenerated"]) == \
        ("6.400", "3.100", "12.500")


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
    assert float(snap.payload["chargedToday"]) == pytest.approx(12.0, abs=0.01)
    assert float(snap.payload["dischargedToday"]) == 0.0
    assert snap.payload["batteryCharge"] == "55.0"


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

    assert float(snap.payload["batteryResultTotal"]) == pytest.approx(
        10.0 + float(snap.payload["batteryResult"]), abs=0.01)


def test_todays_throughput_counts_towards_the_cycle_total(monkeypatch):
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=0.0, battery=1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24), discharge=27.9)

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(cycles_offset=100.0), now=now)

    # 27.9 kWh stored + 12 kWh today, over 27.9 kWh usable, plus the offset.
    assert float(snap.payload["totalBatteryCycles"]) == pytest.approx(101.43, abs=0.01)


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

    assert float(snap.payload["batteryResult"]) == 0.0
    assert float(snap.payload["chargedToday"]) == pytest.approx(12.0, abs=0.01)
    # No prices means the euro figure was suppressed, which the API has a flag for.
    assert snap.payload["invalid"] is True


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
    assert "battery_result=1.23" in line


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


def _fill_setup(monkeypatch, stored_days, filled=18.5):
    calls = []
    monkeypatch.setattr(mb, "_sum_query", lambda *a, **k: 100.0)
    monkeypatch.setattr(mb, "stored_discharge_days", lambda *a, **k: stored_days)
    monkeypatch.setattr(mb, "discharge_from_readings",
                        lambda *a, **k: calls.append(a[3]) or filled)
    return calls


DAYS = [dt.date(2026, 7, d) for d in range(14, 18)]  # 14th .. 17th


def test_yesterday_is_filled_from_power_readings_until_the_nightly_job_runs(monkeypatch):
    """daily_energy lands at ~03:00. Between midnight and then, yesterday is in
    no stored row while today's own discharge has just reset to zero -- so a
    naive sum drops totalBatteryCycles by about a full cycle every night. A
    lifetime counter moving backwards is physically impossible and reads
    downstream as corrupt data, not as a late batch job."""
    calls = _fill_setup(monkeypatch, [DAYS[0], DAYS[1]])  # 14th, 15th; 16th absent
    now = dt.datetime(2026, 7, 17, 0, 30, tzinfo=UTC)     # 02:30 local on the 17th

    total = mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                                      dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC), now=now)

    assert calls == [dt.date(2026, 7, 16)]
    assert total == pytest.approx(118.5)


def test_nothing_is_filled_in_once_the_row_exists(monkeypatch):
    """Otherwise the fix double-counts yesterday from 03:00 onwards, which is a
    worse error than the dip it replaces."""
    calls = _fill_setup(monkeypatch, [DAYS[0], DAYS[1], DAYS[2]])
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)

    total = mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                                      dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC), now=now)

    assert calls == []
    assert total == pytest.approx(100.0)


def test_a_day_whose_row_never_arrives_keeps_being_filled(monkeypatch):
    """Filling ONLY yesterday fixes the nightly dip and leaves a worse bug: a
    day AlphaESS never served, or that efficiency.gate() rejected, is filled
    while it is yesterday and dropped the next midnight -- so the counter does
    not dip and recover, it steps down and stays there."""
    calls = _fill_setup(monkeypatch, [DAYS[0], DAYS[2], DAYS[3]])  # 15th missing
    now = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)  # the 15th is three days back

    mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                              dt.datetime(2026, 7, 17, 22, 0, tzinfo=UTC), now=now)

    assert calls == [dt.date(2026, 7, 15)]


def test_the_fill_is_capped_so_a_broken_nightly_job_is_not_a_stampede(monkeypatch):
    calls = _fill_setup(monkeypatch, [dt.date(2026, 1, 1)])
    now = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    mb.stored_discharge_total(RecordingQueryApi(), "alphaess", "SN",
                              dt.datetime(2026, 7, 17, 22, 0, tzinfo=UTC), now=now,
                              max_fill_days=3)

    # The newest three: the ones still moving the published figure.
    assert calls == [dt.date(2026, 7, 15), dt.date(2026, 7, 16), dt.date(2026, 7, 17)]


def test_days_before_the_first_stored_one_are_not_invented():
    """power_readings may reach further back than daily_energy ever did. Filling
    that stretch would not repair a gap, it would redefine what the lifetime
    total covers -- and silently inflate the published figure."""
    assert mb.missing_discharge_days([dt.date(2026, 7, 16)], dt.date(2026, 7, 17)) == \
        [dt.date(2026, 7, 17)]


def test_no_stored_days_at_all_fills_nothing():
    assert mb.missing_discharge_days([], dt.date(2026, 7, 17)) == []


def test_the_discharge_sum_is_pinned_to_the_current_model_version():
    """efficiency.py supersedes a day by writing a new row at a new version and
    leaving the old one in place. An unfiltered sum counts every recomputed day
    twice -- which on a public leaderboard is a cycle count that roughly doubles
    the first time MODEL_VERSION is bumped, and is invisible until then."""
    import efficiency
    flux = mb._DISCHARGE_TOTAL_FLUX.format(bucket="alphaess", sys_sn="SN",
                                           model_version=mb.ENERGY_MODEL_VERSION,
                                           stop="2026-07-17T00:00:00+00:00")
    assert mb.ENERGY_MODEL_VERSION == efficiency.MODEL_VERSION
    assert f'r.model_version == "{efficiency.MODEL_VERSION}"' in flux


def test_the_fill_integrates_under_the_configured_gap_rule(monkeypatch):
    """A filled day and the live day land in the same payload; integrating them
    under different gap rules makes the two halves disagree."""
    seen = {}

    def record(samples, max_gap_s):
        seen["gap"] = max_gap_s
        return 0.0, 0.0, 0.0

    monkeypatch.setattr(pricing, "load_samples_influx", lambda *a, **k: [])
    monkeypatch.setattr(mb, "battery_energy_kwh", record)

    mb.discharge_from_readings(RecordingQueryApi(), "alphaess", "SN",
                               dt.date(2026, 7, 16), max_gap_s=360.0)

    assert seen["gap"] == 360.0


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


# --------------------------------------------------------------------------
# Totals cache and the midnight rollover
# --------------------------------------------------------------------------

def test_the_cache_is_dropped_at_the_local_midnight(monkeypatch):
    """Both sums mean "everything before today", so their meaning changes at the
    rollover: what was "up to yesterday" becomes "up to the day before". A cache
    warmed at 23:30 and still inside its TTL at 00:05 hands back a total missing
    the whole day that just ended, while today's own discharge has reset to ~0 --
    dropping totalBatteryCycles by a full day, and skipping the fill that exists
    precisely to stop that. A TTL alone cannot see a day boundary."""
    calls = []
    monkeypatch.setattr(mb, "stored_saving_total", lambda *a, **k: calls.append("s") or 1.0)
    monkeypatch.setattr(mb, "stored_discharge_total", lambda *a, **k: calls.append("d") or 2.0)
    totals = mb.Totals(3600)

    before = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)   # today = the 17th
    after = dt.datetime(2026, 7, 17, 22, 0, tzinfo=UTC)    # today = the 18th
    totals.get(FakeQueryApi(), "alphaess", "SN", before, now_mono=1000.0)
    totals.get(FakeQueryApi(), "alphaess", "SN", after, now_mono=1002.0)  # 2 s later

    assert calls == ["s", "d", "s", "d"]


def test_the_cache_still_holds_within_one_day(monkeypatch):
    """The rollover check must not defeat the caching it is bolted onto."""
    calls = []
    monkeypatch.setattr(mb, "stored_saving_total", lambda *a, **k: calls.append("s") or 1.0)
    monkeypatch.setattr(mb, "stored_discharge_total", lambda *a, **k: calls.append("d") or 2.0)
    totals = mb.Totals(3600)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)

    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1000.0)
    totals.get(FakeQueryApi(), "alphaess", "SN", start, now_mono=1300.0)

    assert calls == ["s", "d"]


# --------------------------------------------------------------------------
# Settings that arrive as empty strings
# --------------------------------------------------------------------------

def test_an_empty_setting_falls_back_instead_of_crashing(monkeypatch):
    """docker-compose.yml passes MIJNBATTERIJ_MAX_SAMPLE_GAP_S through as
    `${VAR:-}`, so an unset variable arrives as "" rather than absent -- and
    float("") raises, crash-looping the container at startup over a setting
    nobody touched."""
    monkeypatch.setenv("MIJNBATTERIJ_MAX_SAMPLE_GAP_S", "")
    assert mb._num_env("MIJNBATTERIJ_MAX_SAMPLE_GAP_S", 90.0) == 90.0


def test_the_gap_ceiling_follows_the_poll_interval(monkeypatch):
    """Pinned at 90 while the collector moves to a 120 s poll, every sample pair
    is beyond the ceiling and chargedToday publishes 0.000 forever."""
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("MIJNBATTERIJ_MAX_SAMPLE_GAP_S", "")
    import importlib

    import pricing as pricing_mod
    importlib.reload(pricing_mod)
    reloaded = importlib.reload(mb)
    try:
        assert reloaded.load_config()["max_gap_s"] == 360.0
    finally:
        monkeypatch.undo()
        importlib.reload(pricing_mod)
        importlib.reload(mb)


def test_a_nonsense_setting_warns_and_falls_back(monkeypatch):
    monkeypatch.setenv("MIJNBATTERIJ_TIMEOUT_SECONDS", "soon")
    assert mb._num_env("MIJNBATTERIJ_TIMEOUT_SECONDS", 15.0) == 15.0


# --------------------------------------------------------------------------
# Compose passes through what the code reads
# --------------------------------------------------------------------------

def test_the_service_receives_every_setting_it_reads():
    """A setting documented in .env.example and DEPLOY.md but absent from the
    service's own environment block is a knob the operator turns to no effect.
    POLL_INTERVAL_SECONDS is the dangerous one: it is not a mijnbatterij setting
    at all, but the gap ceiling is derived from it."""
    import pathlib
    import re
    compose = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml"
    block = re.search(r"^  mijnbatterij:$(.*?)^  [a-z]", compose.read_text(), re.M | re.S)
    assert block, "no mijnbatterij service in docker-compose.yml"
    for var in ("MIJNBATTERIJ_MAX_SAMPLE_GAP_S", "POLL_INTERVAL_SECONDS",
                "MIJNBATTERIJ_INTERVAL_SECONDS", "MIJNBATTERIJ_STALE_AFTER_SECONDS",
                "MIJNBATTERIJ_TOTALS_TTL_SECONDS", "MIJNBATTERIJ_CYCLES_OFFSET",
                "BATTERY_CAPACITY_KWH"):
        assert f"{var}:" in block.group(1), f"{var} never reaches the container"


# --------------------------------------------------------------------------
# Telling an empty bucket from a dead collector, across a midnight
# --------------------------------------------------------------------------

class WindowedQueryApi:
    """Answers the newest-sample probe only; collect() patches the loaders."""

    def __init__(self, newest=None):
        self.newest = newest

    def query(self, flux):
        if "last()" not in flux:
            return []

        newest = self.newest

        class Rec:
            @staticmethod
            def get_time():
                return newest

        class Table:
            records = [Rec()] if newest is not None else []

        return [Table()]


def test_an_outage_spanning_midnight_still_reports_stale(monkeypatch):
    """The collector died at 22:00 and stayed dead. From 00:00 today's window is
    empty, so a verdict drawn from that window alone says "no data at all" --
    fresh install, wrong sys_sn, unscoped token -- at exactly the moment the
    outage is longest and the answer is "stale"."""
    now = dt.datetime(2026, 7, 17, 3, 0, tzinfo=UTC)         # 05:00 local
    died = dt.datetime(2026, 7, 16, 20, 0, tzinfo=UTC)       # 22:00 local yesterday
    _stub_influx(monkeypatch, [], [])

    snap, outcome = mb.collect(WindowedQueryApi(newest=died), bucket="alphaess",
                               sys_sn="SN", totals=mb.Totals(3600), config=config(),
                               now=now)

    assert (snap, outcome) == (None, "stale")


def test_a_genuinely_empty_bucket_is_still_no_data(monkeypatch):
    now = dt.datetime(2026, 7, 17, 3, 0, tzinfo=UTC)
    _stub_influx(monkeypatch, [], [])

    assert mb.collect(WindowedQueryApi(newest=None), bucket="alphaess", sys_sn="SN",
                      totals=mb.Totals(3600), config=config(), now=now) == (None, "no-data")


def test_the_seconds_after_midnight_are_not_an_alarm(monkeypatch):
    """Between the local midnight and the day's first poll the window is empty
    while the collector is perfectly healthy. ~30 s wide on a 300 s cycle is
    about one hit every ten days -- a false alarm per fortnight if it were
    treated as a fault."""
    now = dt.datetime(2026, 7, 16, 22, 0, 10, tzinfo=UTC)    # 00:00:10 local
    just_before = dt.datetime(2026, 7, 16, 21, 59, 50, tzinfo=UTC)
    _stub_influx(monkeypatch, [], [])

    assert mb.collect(WindowedQueryApi(newest=just_before), bucket="alphaess",
                      sys_sn="SN", totals=mb.Totals(3600), config=config(),
                      now=now) == (None, "day-start")


# --------------------------------------------------------------------------
# Partial price feed
# --------------------------------------------------------------------------

def test_a_price_feed_that_stopped_mid_day_sends_zero_not_a_fraction(monkeypatch):
    """integrate_by_interval drops energy it has no price for, so a feed that
    stopped at 08:00 returns eight hours of saving for a twenty-hour day -- a
    plausible number, published publicly, with nothing to say it is a third of
    the truth."""
    now = dt.datetime(2026, 7, 17, 18, 0, tzinfo=UTC)        # 20:00 local
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 8))   # 8 h of 20

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(), now=now)

    assert float(snap.payload["batteryResult"]) == 0.0
    assert snap.payload["invalid"] is True
    assert snap.price_coverage == pytest.approx(0.4, abs=0.01)


def test_a_fully_priced_day_still_reports_its_saving(monkeypatch):
    """The coverage check must not swallow the normal case."""
    now = dt.datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
    start = dt.datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
    samples = constant_samples(start, now, grid=1000.0, battery=-1000.0)
    _stub_influx(monkeypatch, samples, hourly_intervals(start, 24))

    snap, _ = mb.collect(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                         totals=mb.Totals(3600), config=config(), now=now)

    assert float(snap.payload["batteryResult"]) != 0.0
    assert "invalid" not in snap.payload
    assert snap.price_coverage == pytest.approx(1.0)


def test_the_price_coverage_reaches_the_status_point():
    """So a zero batteryResult can be told from a genuinely break-even day."""
    now = dt.datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    snap = mb.Snapshot(_payload(), sample(now), age_s=1.0, sample_count=2,
                       price_coverage=0.4)
    assert "price_coverage=0.4" in mb.status_point(snap, "SN", "ok", 200, now).to_line_protocol()


# --------------------------------------------------------------------------
# Empty string settings that are not numbers
# --------------------------------------------------------------------------

def test_an_empty_base_url_falls_back_instead_of_retrying_forever(monkeypatch):
    """`MIJNBATTERIJ_BASE_URL=` leaves base_url "", every POST goes to
    "/api/live" with no scheme, requests raises MissingSchema -- a
    RequestException, so submit() wraps it into SubmitError and the loop retries
    politely forever without naming the setting that is wrong."""
    monkeypatch.setenv("MIJNBATTERIJ_BASE_URL", "")
    assert mb.load_config()["base_url"] == mb.API_BASE


def test_an_empty_mode_setting_means_omit_it(monkeypatch):
    monkeypatch.setenv("MIJNBATTERIJ_MODE", "")
    assert mb.load_config()["mode"] == ""


# --------------------------------------------------------------------------
# A cycle that raised must leave a trace
# --------------------------------------------------------------------------

def _raiser(exc):
    def boom(*a, **k):
        raise exc
    return boom


def _loop_once(monkeypatch, write_api, heartbeats, *, raises=None, cycles=1):
    monkeypatch.setenv("MIJNBATTERIJ_HEARTBEAT_URL", "http://kuma.invalid/push/x")
    monkeypatch.setattr(mb, "send_heartbeat",
                        lambda url, status="up", msg="OK": heartbeats.append((status, msg)))
    if raises is not None:
        def boom(*a, **k):
            raise raises
        monkeypatch.setattr(mb, "run_once", boom)
    mb.run_loop(FakeQueryApi(), write_api, FakeSession(), bucket="alphaess",
                sys_sn="SN", api_key="key", config=config(interval_s=0),
                max_cycles=cycles)


def test_an_influxdb_outage_is_recorded_not_just_logged(monkeypatch):
    """collect() raises before there is anything to submit, so without a row the
    Grafana panel shows a gap identical to the container not running -- which is
    the one question mijnbatterij_submit exists to answer."""
    write_api = RecordingWriteApi()
    _loop_once(monkeypatch, write_api, [], raises=OSError("influxdb unreachable"))

    assert len(write_api.points) == 1
    assert "outcome=error" in write_api.points[0]
    assert "submitted=0" in write_api.points[0]


def test_a_run_of_unexpected_failures_pushes_the_heartbeat_down(monkeypatch):
    """Matching both sibling paths: one blip is noise, a run of them is not."""
    heartbeats = []
    _loop_once(monkeypatch, RecordingWriteApi(), heartbeats,
               raises=OSError("influxdb unreachable"), cycles=2)

    assert [h[0] for h in heartbeats] == ["down"]
    assert "influxdb unreachable" in heartbeats[0][1]


def test_the_first_cycle_after_midnight_does_not_push_down(monkeypatch):
    """day-start is a normal state that clears itself within one poll interval;
    treating it as a fault is a false alarm roughly every ten days."""
    heartbeats = []
    monkeypatch.setattr(mb, "run_once", lambda *a, **k: (None, "day-start"))
    _loop_once(monkeypatch, RecordingWriteApi(), heartbeats)

    assert heartbeats == []


def test_an_outage_does_push_down(monkeypatch):
    heartbeats = []
    monkeypatch.setattr(mb, "run_once", lambda *a, **k: (None, "stale"))
    _loop_once(monkeypatch, RecordingWriteApi(), heartbeats)

    assert heartbeats == [("down", "nothing submitted: stale")]


def test_a_payload_rejection_says_it_will_not_clear_on_its_own(monkeypatch, caplog):
    """A 400 naming an invalid setting is retried every 300 s forever and logs
    identically to a transient blip, so the log gives the reader no reason to
    stop waiting for it to pass. Observed live: `400 Battery mode:
    self_consumption is not available for frank-energie`."""
    monkeypatch.setattr(mb, "run_once", _raiser(mb.SubmitError("HTTP 400: mode", 400)))
    with caplog.at_level("ERROR"):
        _loop_once(monkeypatch, RecordingWriteApi(), [])
    assert "will repeat until a setting changes" in caplog.text
    assert "MIJNBATTERIJ_MODE" in caplog.text


def test_a_server_error_is_not_called_permanent(monkeypatch, caplog):
    """A 500 or a timeout is worth simply trying again in five minutes, and
    telling the operator to go edit .env would send them after nothing."""
    monkeypatch.setattr(mb, "run_once", _raiser(mb.SubmitError("HTTP 503: busy", 503)))
    with caplog.at_level("ERROR"):
        _loop_once(monkeypatch, RecordingWriteApi(), [])
    assert "will repeat until a setting changes" not in caplog.text


def test_a_rate_limit_is_not_called_permanent(monkeypatch, caplog):
    monkeypatch.setattr(mb, "run_once", _raiser(mb.SubmitError("HTTP 429: slow down", 429)))
    with caplog.at_level("ERROR"):
        _loop_once(monkeypatch, RecordingWriteApi(), [])
    assert "will repeat until a setting changes" not in caplog.text


# --------------------------------------------------------------------------
# Monthly backfill
# --------------------------------------------------------------------------

def _stub_month(monkeypatch, energy_rows, saving_rows, readings=(0.0, 0.0)):
    def rows(query_api, bucket, meas, sys_sn, model_version, fields, start, stop):
        return energy_rows if meas == "daily_energy" else saving_rows
    monkeypatch.setattr(mb, "_daily_rows", rows)
    monkeypatch.setattr(mb, "energy_from_readings", lambda *a, **k: readings)


def _energy(day, charged, discharged):
    return {day: {"charge_kwh_api": charged, "discharge_kwh_api": discharged}}


def test_a_month_returns_one_row_per_day(monkeypatch):
    energy = {**_energy(dt.date(2026, 8, 1), 29.2, 26.6),
              **_energy(dt.date(2026, 8, 2), 29.7, 25.3)}
    savings = {dt.date(2026, 8, 1): {"saving": 4.3435},
               dt.date(2026, 8, 2): {"saving": 4.3372}}
    _stub_month(monkeypatch, energy, savings)

    rows, report = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                                  year=2026, month=8, until=dt.date(2026, 8, 2))

    assert [r["day"] for r in rows] == [dt.date(2026, 8, 1), dt.date(2026, 8, 2)]
    assert rows[0]["charged"] == 29.2 and rows[0]["result"] == 4.3435
    assert report["filled"] == [] and report["unpriced"] == []
    assert report["expected"] == 2


def test_today_is_never_sent_as_a_finished_day(monkeypatch):
    """Today is still moving and is what /api/live is for. The spec agrees: the
    daily endpoint's `date` must be before today in Europe/Amsterdam."""
    energy = {**_energy(dt.date(2026, 8, 30), 1.0, 1.0),
              **_energy(dt.date(2026, 8, 31), 2.0, 2.0)}
    _stub_month(monkeypatch, energy, {})

    rows, _ = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                             year=2026, month=8, until=dt.date(2026, 8, 30))

    # The 31st is in `energy` and would appear but for the `until` cap, so this
    # fails if the cap is removed rather than passing on absence.
    assert [r["day"] for r in rows] == [dt.date(2026, 8, 30)]


def test_a_month_with_no_finished_days_yet_is_empty(monkeypatch):
    """The 1st of a month: `until` is yesterday, which is last month."""
    _stub_month(monkeypatch, {}, {})
    rows, _ = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                             year=2026, month=9, until=dt.date(2026, 8, 31))
    assert rows == []


def test_a_day_without_a_daily_energy_row_is_integrated_from_readings(monkeypatch):
    """Those gaps are real -- five days in August 2026 -- and a month total that
    silently omits them is wrong rather than merely incomplete."""
    _stub_month(monkeypatch, {}, {dt.date(2026, 8, 17): {"saving": 2.5}},
                readings=(20.0, 18.0))

    rows, report = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                                  year=2026, month=8, until=dt.date(2026, 8, 17))

    assert rows[0]["charged"] == 20.0 and rows[0]["discharged"] == 18.0
    assert rows[0]["derived"] is True
    assert dt.date(2026, 8, 17) in report["filled"]


def test_a_derived_day_is_flagged_invalid_when_sent(monkeypatch):
    """The figures rest on the derived series rather than AlphaESS's own
    totals, and the API has a field that says exactly that."""
    payload = mb.build_daily(day=dt.date(2026, 8, 17), charged_kwh=20.0,
                             discharged_kwh=18.0, result=2.5, invalid=True)
    assert payload["invalid"] is True
    assert payload["date"] == "2026-08-17"


def test_a_day_with_no_data_anywhere_is_left_out_entirely(monkeypatch):
    """Sending it as a zero day reads as "the battery did nothing" rather than
    "we were not watching", and the platform cannot tell those apart."""
    _stub_month(monkeypatch, {}, {}, readings=(0.0, 0.0))

    rows, report = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                                  year=2026, month=8, until=dt.date(2026, 8, 3))

    assert rows == []
    assert report["filled"] == []


def test_a_gated_day_is_sent_as_zero_euros_flagged_invalid(monkeypatch):
    """Recomputing it ungated would put a number on a public leaderboard that no
    stored row can ever be reconciled against -- the same call
    stored_saving_total makes. Observed live: 2026-08-29, coverage 0.808."""
    _stub_month(monkeypatch, _energy(dt.date(2026, 8, 29), 22.0, 19.0), {})

    rows, report = mb.build_month(FakeQueryApi(), bucket="alphaess", sys_sn="SN",
                                  year=2026, month=8, until=dt.date(2026, 8, 29))

    assert rows[0]["result"] is None
    assert report["unpriced"] == [dt.date(2026, 8, 29)]

    payload = mb.build_daily(day=rows[0]["day"], charged_kwh=rows[0]["charged"],
                             discharged_kwh=rows[0]["discharged"], result=None)
    assert payload["batteryResult"] == "0.00"
    assert payload["invalid"] is True
    assert payload["dischargedToday"] == "19.000"


def test_the_month_totals_carry_no_per_day_structure():
    """`days` was invented -- the spec has no such field, and sending it is what
    earned `400 Invalid request provided`."""
    payload = mb.build_month_totals(year=2026, month=8, charged_kwh=747.4,
                                    discharged_kwh=713.5, result=113.47,
                                    partial=True)
    assert "days" not in payload
    assert payload["yearMonth"] == "2026-08"
    assert payload["batteryResult"] == "113.47"
    assert payload["batteryCharged"] == "747.400"
    assert payload["partial"] is True


def test_a_complete_month_is_not_marked_partial():
    payload = mb.build_month_totals(year=2026, month=8, charged_kwh=1.0,
                                    discharged_kwh=1.0, result=1.0, partial=False)
    assert "partial" not in payload


def test_a_day_result_is_never_finalized():
    """The spec says a finalized result can never be updated, and a day here can
    legitimately improve once its nightly daily_cost row lands."""
    assert "finalized" not in mb.build_daily(
        day=dt.date(2026, 8, 1), charged_kwh=1.0, discharged_kwh=1.0, result=1.0)


def test_the_daily_payload_goes_to_the_daily_endpoint():
    session = FakeSession(FakeResponse(200))
    mb.submit_daily(session, "key-123", {"date": "2026-08-01"})
    assert session.posts[0]["url"] == "https://api.mijnbatterij.nl/api/results/daily"


def test_a_mode_outside_the_published_enum_is_refused(monkeypatch):
    """Rather than sent, where it comes back as a 400 every cycle for as long as
    it takes someone to read the log."""
    monkeypatch.setenv("MIJNBATTERIJ_MODE", "doe_het_zelf")
    assert mb.load_config()["mode"] == ""


def test_every_published_mode_is_accepted(monkeypatch):
    for mode in mb.MODES:
        monkeypatch.setenv("MIJNBATTERIJ_MODE", mode)
        assert mb.load_config()["mode"] == mode


def test_the_monthly_payload_goes_to_the_results_endpoint():
    session = FakeSession(FakeResponse(200))
    mb.submit_monthly(session, "key-123", {"yearMonth": "2026-08"})
    assert session.posts[0]["url"] == "https://api.mijnbatterij.nl/api/results/monthly"
    assert session.posts[0]["headers"]["Authorization"] == "Bearer key-123"


def test_the_live_endpoint_is_unchanged_by_the_refactor():
    session = FakeSession(FakeResponse(200))
    mb.submit(session, "key-123", {"batteryCharge": 50})
    assert session.posts[0]["url"] == "https://api.mijnbatterij.nl/api/live"


# --------------------------------------------------------------------------
# Heartbeat query string
# --------------------------------------------------------------------------

class _RecordingGet:
    """Stands in for `requests.get`, keeping every URL it was handed."""

    def __init__(self, exc: Exception | None = None):
        self.urls: list[str] = []
        self.exc = exc

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return FakeResponse(200, "")


KUMA_URL = "http://data42.lan:3001/api/push/E1UNtJJr3h?status=up&msg=OK&ping="


def test_the_heartbeat_replaces_kumas_own_query_string(monkeypatch):
    """The URL Kuma displays already carries `?status=up&msg=OK&ping=`, and that whole
    string is what an operator pastes into .env. Appending to it makes Express parse
    `status` as an array matching neither "up" nor "down", so every ping -- `up` ones
    included -- registers DOWN. Twice fixed elsewhere in this repo before this module
    reintroduced it: collector/collector.py:407 and dispatch/heartbeat.py:22."""
    get = _RecordingGet()
    monkeypatch.setattr(mb.requests, "get", get)
    mb.send_heartbeat(KUMA_URL, "down", "HTTP 400: rejected")
    assert len(get.urls) == 1
    query = urlsplit(get.urls[0]).query
    assert parse_qs(query)["status"] == ["down"], f"status must not repeat: {query}"
    assert parse_qs(query)["msg"] == ["HTTP 400: rejected"]
    assert get.urls[0].startswith("http://data42.lan:3001/api/push/E1UNtJJr3h?")


def test_the_heartbeat_swallows_a_malformed_url(monkeypatch):
    """A bad URL in .env is a monitoring problem. It must not stop submissions."""
    get = _RecordingGet(requests.exceptions.MissingSchema("no scheme"))
    monkeypatch.setattr(mb.requests, "get", get)
    mb.send_heartbeat("data42:3001/api/push/abc")  # no exception


def test_an_empty_heartbeat_url_pings_nothing(monkeypatch):
    get = _RecordingGet()
    monkeypatch.setattr(mb.requests, "get", get)
    mb.send_heartbeat("")
    assert get.urls == []
