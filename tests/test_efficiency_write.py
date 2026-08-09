"""What the nightly run writes, and what it refuses to write.

Drives the real `run_influx` rather than a reimplementation, because the
interesting behaviour is in how it sequences things: the raw series is written
even for a day whose derived figures are rejected, and a day already done is
never fetched at all -- a rate-limited API makes that a correctness property,
not an optimisation.
"""

import datetime as dt

import pytest

import efficiency
import pricing
from conftest import constant_samples

DAY = dt.date(2026, 8, 5)
STEPS = 288
ENERGY = {"eCharge": 27.3, "eDischarge": 17.8, "epv": 17.71,
          "eOutput": 11.55, "eInput": 22.82, "eGridCharge": 16.9}


class FakeWriteApi:
    def __init__(self):
        self.batches = []

    def write(self, bucket=None, record=None):
        self.batches.append(record if isinstance(record, list) else [record])

    def points(self, measurement):
        return [p for batch in self.batches for p in batch
                if p._name == measurement]


class FakeQueryApi:
    def query(self, flux):
        return []


class FakeInfluxClient:
    def __init__(self, write_api):
        self._write_api = write_api
        self.closed = False

    def write_api(self, write_options=None):
        return self._write_api

    def query_api(self):
        return FakeQueryApi()

    def close(self):
        self.closed = True


def metered_records(day, loads, soc=50.0):
    start = dt.datetime.combine(day, dt.time())
    return [
        {"uploadTime": (start + dt.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"),
         "load": load, "cbat": soc, "feedIn": 0.0, "gridCharge": 0.0}
        for i, load in enumerate(loads)
    ]


@pytest.fixture
def harness(monkeypatch):
    """A run wired to fakes at exactly the seams production uses."""
    for key, value in {
        "INFLUX_URL": "http://influxdb:8086", "INFLUX_TOKEN": "t",
        "INFLUX_ORG": "home", "INFLUX_BUCKET": "alphaess",
        "ALPHAESS_APP_ID": "id", "ALPHAESS_APP_SECRET": "secret",
        "ALPHAESS_SYS_SN": "SN1",
    }.items():
        monkeypatch.setenv(key, value)

    write_api = FakeWriteApi()
    state = {
        "write_api": write_api,
        "beats": [],
        "power_calls": [],
        "already_done": False,
        "records": metered_records(DAY, [900.0] * STEPS),
        "energy": dict(ENERGY),
    }
    monkeypatch.setattr(efficiency, "InfluxDBClient",
                        lambda url, token, org: FakeInfluxClient(write_api))
    monkeypatch.setattr(efficiency, "send_heartbeat",
                        lambda url, status="up", msg="OK":
                        state["beats"].append((status, msg)))
    monkeypatch.setattr(efficiency, "HEARTBEAT_URL", "https://kuma.example/api/push/abc")
    monkeypatch.setattr(efficiency, "_already_done",
                        lambda *a, **k: state["already_done"])

    def fake_power(app_id, app_secret, sys_sn, day):
        state["power_calls"].append(day)
        return state["records"]

    monkeypatch.setattr(efficiency, "fetch_day_power", fake_power)
    monkeypatch.setattr(efficiency, "fetch_day_energy",
                        lambda *a, **k: state["energy"])

    def fake_readings(query_api, bucket, sys_sn, start, end):
        parsed = efficiency.parse_upload_times(state["records"], DAY)
        if not parsed:
            return []
        return constant_samples(parsed[0][0], parsed[-1][0], grid=1000.0)

    monkeypatch.setattr(efficiency, "load_samples_influx", fake_readings)
    return state


def run(state, **kwargs):
    kwargs.setdefault("dry_run", False)
    kwargs.setdefault("force", False)
    return efficiency.run_influx([DAY], **kwargs)


def test_a_good_day_writes_both_measurements(harness):
    summary = run(harness)
    write_api = harness["write_api"]
    assert len(write_api.points("metered_power")) == STEPS
    assert len(write_api.points("daily_energy")) == 1
    assert summary.written == [DAY]


def test_the_daily_row_is_tagged_and_stamped_at_local_midnight(harness):
    run(harness)
    point = harness["write_api"].points("daily_energy")[0]
    assert point._tags == {"sys_sn": "SN1", "model_version": efficiency.MODEL_VERSION}
    assert point._time == pricing.day_window_utc(DAY)[0]


def test_every_daily_row_carries_computed_at_unix(harness):
    """All three staleness checks read this field. A row without it is
    invisible to the monitoring, which is worse than a missing row."""
    run(harness)
    point = harness["write_api"].points("daily_energy")[0]
    assert "computed_at_unix" in point._fields


def test_dry_run_writes_nothing(harness):
    run(harness, dry_run=True)
    assert harness["write_api"].batches == []


def test_a_gated_day_still_stores_the_raw_series(harness, monkeypatch):
    """Raw upstream data is worth keeping on a day whose derived figures are
    not -- it is what makes a recompute possible without re-hitting an API that
    rate-limits."""
    monkeypatch.setattr(efficiency, "MAX_SOC_ALIGN_PP", 0.0001)
    harness["records"] = metered_records(DAY, [900.0] * STEPS, soc=99.0)
    summary = run(harness)

    write_api = harness["write_api"]
    assert len(write_api.points("metered_power")) == STEPS
    assert write_api.points("daily_energy") == []
    assert summary.written == []
    assert [d for d, _ in summary.gated] == [DAY]


def test_an_already_done_day_is_never_fetched(harness):
    """Not an optimisation: every avoided call is rate budget the live 30 s
    poll loop gets to keep."""
    harness["already_done"] = True
    summary = run(harness)
    assert harness["power_calls"] == []
    assert summary.skipped == [DAY]


def test_force_reprocesses_an_already_done_day(harness):
    harness["already_done"] = True
    summary = run(harness, force=True)
    assert harness["power_calls"] == [DAY]
    assert summary.written == [DAY]


def test_a_blank_payload_is_never_stored_as_a_day_of_zeros(harness):
    """AlphaESS returns HTTP 200 / code 200 with all-zero totals around local
    midnight. Written once, the day is marked done and never revisited."""
    harness["energy"] = dict.fromkeys(efficiency.ENERGY_KEYS, 0.0)
    summary = run(harness)
    assert harness["write_api"].batches == []
    assert summary.empty == [DAY]


def test_a_throttled_day_writes_nothing_and_is_recorded(harness, monkeypatch):
    def boom(*a, **k):
        raise efficiency.ThrottledError("getOneDayPowerBySn: code 6053 after 4 retries")

    monkeypatch.setattr(efficiency, "fetch_day_power", boom)
    summary = run(harness)
    assert harness["write_api"].batches == []
    assert summary.throttled == [DAY]


def test_the_circuit_breaker_stops_a_hammering_backfill(harness, monkeypatch):
    """A backfill that keeps pounding a throttled API is worse than one that
    stops and gets re-run tomorrow -- it also starves the live collector."""
    attempted = []

    def boom(app_id, app_secret, sys_sn, day):
        attempted.append(day)
        raise efficiency.ThrottledError("code 6053 after 4 retries")

    monkeypatch.setattr(efficiency, "fetch_day_power", boom)
    days = [DAY - dt.timedelta(days=n) for n in range(10, 0, -1)]
    summary = efficiency.run_influx(days, dry_run=False, force=True)

    assert len(attempted) == efficiency.THROTTLE_CIRCUIT_BREAK
    assert len(summary.throttled) == efficiency.THROTTLE_CIRCUIT_BREAK


def test_a_fatal_api_error_skips_the_day_without_retrying_the_rest(harness, monkeypatch):
    """Bad credentials do not get better by being asked again."""
    monkeypatch.setattr(efficiency, "fetch_day_power",
                        lambda *a, **k: (_ for _ in ()).throw(
                            efficiency.ApiError("getOneDayPowerBySn: code 6002 (invalid sign)")))
    summary = run(harness)
    assert summary.failed and summary.failed[0][0] == DAY
    assert summary.throttled == []


def test_the_client_is_closed_even_when_a_day_explodes(harness, monkeypatch):
    clients = []

    def make(url, token, org):
        client = FakeInfluxClient(harness["write_api"])
        clients.append(client)
        return client

    monkeypatch.setattr(efficiency, "InfluxDBClient", make)
    monkeypatch.setattr(efficiency, "compute_day",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run(harness)
    assert clients[0].closed
