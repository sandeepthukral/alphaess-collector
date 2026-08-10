"""The poll loop must distinguish an AlphaESS failure from an InfluxDB failure.

Both halves of a poll raise the same exception types, and the diagnosis that
rides along with the alert is only useful if it names the right one. Getting
this backwards is worse than saying nothing: the "upstream" verdict explicitly
tells the operator there is nothing to fix.

These tests drive the real `run_loop` rather than a reimplementation of it,
because the bug being pinned was in how the loop scoped its try block.
"""


import pytest

import collector


class StopLoop(Exception):
    """Raised from the patched sleep to leave run_loop after N polls."""


class FakeWriteApi:
    def __init__(self, fail_with=None, fail_first=0):
        self.fail_with = fail_with
        self.fail_first = fail_first  # fail only the first N writes, then heal
        self.calls = 0
        self.points = []

    def write(self, bucket=None, record=None):
        self.calls += 1
        failing = self.fail_with is not None and (
            self.fail_first == 0 or self.calls <= self.fail_first)
        if failing:
            raise self.fail_with
        self.points.append(record)


class FakeInfluxClient:
    def __init__(self, write_api):
        self._write_api = write_api
        self.closed = False

    def write_api(self, write_options=None):
        return self._write_api

    def close(self):
        self.closed = True


@pytest.fixture
def loop_env(monkeypatch):
    for key, value in {
        "INFLUX_URL": "http://influxdb:8086",
        "INFLUX_TOKEN": "test-token",
        "INFLUX_ORG": "home",
        "INFLUX_BUCKET": "alphaess",
        "POLL_INTERVAL_SECONDS": "30",
        "HEARTBEAT_URL": "https://kuma.example/api/push/abc123?status=up&msg=OK&ping=",
    }.items():
        monkeypatch.setenv(key, value)
    # Installing real signal handlers under pytest would outlive the test.
    monkeypatch.setattr(collector.signal, "signal", lambda *a, **kw: None)
    monkeypatch.setattr(collector, "check_mtu", lambda *a, **kw: None)


@pytest.fixture
def harness(monkeypatch, loop_env):
    """Run run_loop for a bounded number of polls and capture what it decided."""
    state = {
        "polls": 0,
        "heartbeats": [],
        "health_events": [],
        "diagnose_network_calls": 0,
        "diagnose_write_calls": 0,
        "clock": 0.0,
    }

    monkeypatch.setattr(collector, "send_heartbeat",
                        lambda url, status="up", msg="OK", timeout=5:
                        state["heartbeats"].append((status, msg)))

    def fake_health_event(write_api, bucket, sys_sn, event, fields,
                          error_class=None, stage=None):
        state["health_events"].append(
            {"event": event, "error_class": error_class, "stage": stage, **fields})

    monkeypatch.setattr(collector, "write_health_event", fake_health_event)

    def fake_diagnose_network(expected_max_mtu, control_url):
        state["diagnose_network_calls"] += 1
        return "upstream"  # what the probes report when only InfluxDB is down

    def fake_diagnose_write(influx_url):
        state["diagnose_write_calls"] += 1
        return "local-influxdb"

    monkeypatch.setattr(collector, "diagnose_network", fake_diagnose_network)
    monkeypatch.setattr(collector, "diagnose_write", fake_diagnose_write)

    # A fake clock so the backoff sleep does not take real minutes.
    def fake_monotonic():
        state["clock"] += 1.0
        return state["clock"]

    def fake_sleep(_seconds):
        if state["polls"] >= state["stop_after"]:
            raise StopLoop

    monkeypatch.setattr(collector.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(collector.time, "sleep", fake_sleep)

    def run(*, fetch, write_fails_with=None, write_fails_first=0, stop_after=3):
        state["stop_after"] = stop_after
        write_api = FakeWriteApi(fail_with=write_fails_with,
                                 fail_first=write_fails_first)
        monkeypatch.setattr(collector, "InfluxDBClient",
                            lambda url, token, org: FakeInfluxClient(write_api))

        def counting_fetch(app_id, app_secret, sys_sn):
            state["polls"] += 1
            return fetch(state["polls"])

        monkeypatch.setattr(collector, "get_last_power_data", counting_fetch)
        with pytest.raises(StopLoop):
            collector.run_loop("app-id", "app-secret", "AL5000TESTSN")
        state["write_api"] = write_api
        return state

    return run


GOOD_SAMPLE = {"ppv": 1500, "pgrid": -200, "pload": 800, "pbat": -500, "soc": 87.5}


def healthy(_poll):
    return dict(GOOD_SAMPLE)


def unreachable_api(_poll):
    raise ConnectionError(
        "HTTPSConnectionPool(host='openapi.alphaess.com', port=443): Max retries "
        "exceeded (Caused by SSLError(SSLEOFError(8, 'EOF in violation of protocol')))")


# --------------------------------------------------------------------------

def test_influxdb_write_failure_is_not_blamed_on_alphaess(harness):
    """Regression: an InfluxDB outage was diagnosed as "upstream".

    The fetch succeeds, so DNS for the API host resolves and the unrelated
    HTTPS control endpoint answers -- diagnose_network sees a healthy network
    and concludes the fault is at AlphaESS, telling the operator there is
    "nothing to fix here" while every sample is being dropped on the floor.
    """
    state = harness(fetch=healthy,
                    write_fails_with=ConnectionError("Max retries exceeded: influxdb:8086"))

    assert state["diagnose_network_calls"] == 0, \
        "network probes are meaningless for a write failure and mislead"
    assert state["diagnose_write_calls"] == 1

    down = [msg for status, msg in state["heartbeats"] if status == "down"]
    assert down, "a write failure must still trip the heartbeat"
    assert all(msg.startswith("write:") for msg in down)
    assert "[local-influxdb]" in down[-1]
    assert "upstream" not in down[-1]


def test_write_failures_are_tagged_as_the_write_stage(harness):
    state = harness(fetch=healthy,
                    write_fails_with=ConnectionError("influxdb unreachable"))
    failures = [e for e in state["health_events"] if e["event"] == "failure"]
    assert failures
    assert all(e["stage"] == "write" for e in failures)


def test_api_failure_still_runs_the_network_diagnosis(harness):
    """The pre-existing behaviour must survive the split."""
    state = harness(fetch=unreachable_api)

    assert state["diagnose_network_calls"] == 1
    assert state["diagnose_write_calls"] == 0

    down = [msg for status, msg in state["heartbeats"] if status == "down"]
    assert all(msg.startswith("fetch:") for msg in down)
    assert "[upstream]" in down[-1]

    failures = [e for e in state["health_events"] if e["event"] == "failure"]
    assert all(e["stage"] == "fetch" for e in failures)
    assert failures[0]["error_class"] == "ConnectionError"


def test_diagnosis_runs_once_per_outage_not_per_failure(harness):
    state = harness(fetch=unreachable_api, stop_after=8)
    assert state["polls"] >= 8
    assert state["diagnose_network_calls"] == 1


def test_no_heartbeat_down_on_a_single_failure(harness):
    """One failed poll is usually a blip the next poll rides out."""
    state = harness(fetch=unreachable_api, stop_after=1)
    assert [status for status, _ in state["heartbeats"]] == []


def test_healthy_poll_writes_a_point_and_pings_up(harness):
    state = harness(fetch=healthy, stop_after=2)
    assert state["write_api"].points
    assert [status for status, _ in state["heartbeats"]] == ["up", "up"]
    assert state["health_events"] == [], "healthy polls write no health events"


def test_recovery_after_an_api_outage_is_recorded(harness):
    def flaky(poll):
        if poll <= 3:
            return unreachable_api(poll)
        return dict(GOOD_SAMPLE)

    state = harness(fetch=flaky, stop_after=5)
    recovered = [e for e in state["health_events"] if e["event"] == "recovered"]
    assert len(recovered) == 1
    assert recovered[0]["failures"] == 3
    assert recovered[0]["outage_seconds"] > 0

    up = [msg for status, msg in state["heartbeats"] if status == "up"]
    assert "recovered after 3 failures" in up[0]


def test_the_recovery_heartbeat_names_what_broke(harness):
    """The "up" message is the one that survives a link outage; see
    recovery_message. Kuma could not deliver a single "down" notification on
    2026-08-10 because sending one needed the DNS that had just failed, so
    everything the operator learns has to be in here."""
    def flaky(poll):
        if poll <= 3:
            return unreachable_api(poll)
        return dict(GOOD_SAMPLE)

    state = harness(fetch=flaky, stop_after=5)
    up = [msg for status, msg in state["heartbeats"] if status == "up"]

    assert "fetch: ConnectionError" in up[0], \
        "the recovery message must name the stage and the error"
    assert "[upstream]" in up[0], \
        "and the verdict, which is computed on a later poll than the failure"


def test_a_healed_outage_does_not_haunt_later_recoveries(harness):
    """The cause is loop-scoped state like `verdict` and `stage` before it: a
    second, unrelated outage must not be reported with the first one's error,
    and a healthy ping must carry no cause at all."""
    def flaky(poll):
        if poll in (2, 3):
            return unreachable_api(poll)
        return dict(GOOD_SAMPLE)

    state = harness(fetch=flaky, stop_after=6)
    up = [msg for status, msg in state["heartbeats"] if status == "up"]

    assert up[0] == "OK", "the first poll never failed"
    recovery = [msg for msg in up if "recovered" in msg]
    assert len(recovery) == 1
    assert "fetch: ConnectionError" in recovery[0]
    assert [msg for msg in up[up.index(recovery[0]) + 1:]] == ["OK"] * (
        len(up) - up.index(recovery[0]) - 1), \
        "polls after the recovery are plain OK, not a stale error"


def test_stage_resets_once_the_write_recovers(harness):
    """A run of write failures must not leave later polls mislabelled.

    `stage` is loop-local state; if it were hoisted out of the loop body a
    healed InfluxDB would keep every subsequent failure tagged "write".
    """
    state = harness(fetch=healthy,
                    write_fails_with=ConnectionError("influxdb unreachable"),
                    write_fails_first=2, stop_after=4)

    failures = [e for e in state["health_events"] if e["event"] == "failure"]
    recovered = [e for e in state["health_events"] if e["event"] == "recovered"]
    assert [e["stage"] for e in failures] == ["write", "write"]
    assert len(recovered) == 1
    assert recovered[0]["failures"] == 2
    # Once healthy again, points land and the heartbeat goes back up.
    assert state["write_api"].points
    assert state["heartbeats"][-1][0] == "up"
