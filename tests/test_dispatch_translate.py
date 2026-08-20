"""The translator job: InfluxDB -> slots.json, plus its two monitors. `dispatch/translate.py`.

Everything here is about the boundary, not the algorithm -- `test_dispatch_translator.py` and
`test_dispatch_goldens.py` own what the slots should contain. What this file defends is the
handful of properties an operator depends on at 03:00:

  - a failed run leaves the previous slots.json intact,
  - a reader never sees a half-written file,
  - and the monitor that goes down names the cause, rather than going silent.

The ping itself is `dispatch/heartbeat.py`, shared with the control loop and tested in
`test_dispatch_monitors.py`. Here it is faked, so these tests are about WHICH monitor speaks
and WHEN, never about the HTTP.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import translate as T
from plan import PlanFormatError

UTC = dt.UTC
NOW = dt.datetime(2026, 8, 16, 12, 7, tzinfo=UTC)

FIELDS = {
    "soc_wh": 14000.0, "charge_wh": 0.0, "discharge_wh": 0.0,
    "import_wh": 100.0, "export_wh": 0.0, "price_buy": 0.30, "price_sell": 0.10,
    "cost_eur": 0.03, "pv_forecast_wh": 0.0,
}


class FakeRecord:
    def __init__(self, time: dt.datetime, values: dict):
        self._time, self.values = time, values

    def get_time(self):
        return self._time


class FakeTable:
    def __init__(self, records):
        self.records = records


class FakeQueryApi:
    """Enough of `influxdb_client`'s query API for `from_influx`. Records the Flux it was given
    so a test can assert the window, which is otherwise invisible."""

    def __init__(self, records, error: Exception | None = None):
        self.records, self.error, self.flux = records, error, ""

    def query(self, flux):
        self.flux = flux
        if self.error:
            raise self.error
        return [FakeTable(self.records)]


def records(start: dt.datetime, count: int, plan_run: str = "2026-08-16T12:05:00Z",
            **overrides) -> list[FakeRecord]:
    """`count` consecutive quarter-hour points from the quarter containing `start`.

    Floored to the quarter hour because real plan points are: the planner emits 00/15/30/45,
    and a fixture on 07/22/37 would make the trim tests pass or fail for reasons the
    production data cannot reproduce.
    """
    start = start.replace(minute=start.minute // 15 * 15, second=0, microsecond=0)
    out = []
    for i in range(count):
        values = {"plan_run": plan_run, **FIELDS, **overrides}
        out.append(FakeRecord(start + dt.timedelta(minutes=15 * i), values))
    return out


def api(*args, **kwargs) -> FakeQueryApi:
    return FakeQueryApi(records(*args, **kwargs))


class TestTranslate:
    def test_a_plan_becomes_a_slots_document(self):
        doc, warnings = T.translate(api(NOW, 8), "planning", NOW, 27900.0)
        assert warnings == []
        assert doc["plan_run"] == "2026-08-16T12:05:00Z"
        assert doc["interval_minutes"] == 15
        assert doc["slots"], "a plan with eight intervals produced no slots"

    def test_finished_intervals_are_dropped(self):
        """The query deliberately reaches an hour back so the CURRENT interval is never missed.
        Those extra intervals must not reach the file."""
        start = NOW - dt.timedelta(hours=1)
        doc, _ = T.translate(FakeQueryApi(records(start, 12)), "planning", NOW, 27900.0)
        first = dt.datetime.fromisoformat(doc["slots"][0]["start"].replace("Z", "+00:00"))
        # 12:07 sits inside the 12:00-12:15 interval, which is still actionable.
        assert first == dt.datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def test_the_current_interval_is_kept_even_when_nearly_over(self):
        now = dt.datetime(2026, 8, 16, 12, 14, 59, tzinfo=UTC)
        doc, _ = T.translate(FakeQueryApi(records(now - dt.timedelta(hours=1), 12)),
                             "planning", now, 27900.0)
        assert doc["slots"][0]["start"] == "2026-08-16T12:00:00Z"

    def test_a_plan_that_ends_in_the_past_is_an_error_not_an_empty_file(self):
        """The dangerous failure: an empty slots file parses fine and takes the battery quiet
        with every monitor green."""
        stale = records(NOW - dt.timedelta(hours=1), 2)
        with pytest.raises(PlanFormatError, match="planner has not run since"):
            T.translate(FakeQueryApi(stale), "planning", NOW, 27900.0)

    def test_a_lone_trailing_interval_blames_the_planner_not_the_cadence(self):
        """`upcoming()` infers the cadence, and `interval_minutes()` needs two intervals to
        see a gap at all. So a window down to its last interval used to reach monitor #2 as
        "need at least two intervals to infer the cadence" -- which reads as a malformed plan
        and sends the operator to the other repo, for what is really a planner that stopped.

        The neighbouring empty case is not a duplicate of this one: `from_influx` catches zero
        points first, with a message naming the bucket and the range."""
        with pytest.raises(PlanFormatError, match="planner has not run since"):
            T.translate(FakeQueryApi(records(NOW, 1)), "planning", NOW, 27900.0)

    def test_a_plan_down_to_its_last_interval_blames_the_planner_too(self):
        """The adjacent boundary to the lone-interval case, and it arrives by a different
        road: the window holds a translatable plan, but all of it except the final interval
        has already elapsed. That is a planner hours overdue, and the message has to say
        so rather than reporting the cadence it can no longer infer from one interval."""
        lapsing = records(NOW - dt.timedelta(hours=1), 5)  # only the last still ends after NOW
        with pytest.raises(PlanFormatError, match="planner has not run since"):
            T.translate(FakeQueryApi(lapsing), "planning", NOW, 27900.0)

    def test_an_empty_window_names_the_bucket_and_the_range(self):
        with pytest.raises(PlanFormatError, match="no plan points in bucket 'planning'"):
            T.translate(FakeQueryApi([]), "planning", NOW, 27900.0)

    def test_a_hole_in_the_elapsed_lookback_does_not_abort_a_good_future(self):
        """The cadence used for the trim comes from the tail, not the whole window.

        The query reaches an hour back, and `interval_minutes()` rejects a window whose gaps
        disagree -- so a single point missing from that already-elapsed hour used to abort a
        plan whose entire future was intact. The strictness still applies to what survives the
        trim, which is the part the dispatcher actually follows."""
        holed = records(NOW - dt.timedelta(hours=1), 12)
        del holed[1]  # 11:15 never made it into Influx; 11:00 -> 11:30 is now a 30 min gap
        doc, _ = T.translate(FakeQueryApi(holed), "planning", NOW, 27900.0)
        assert doc["slots"][0]["start"] == "2026-08-16T12:00:00Z"
        assert doc["interval_minutes"] == 15

    def test_a_gap_in_the_dispatchable_window_is_still_an_error(self):
        """The other side of that boundary: a hole AFTER `now` is a plan the dispatcher would
        act on, and it has to fail rather than be quietly stepped over."""
        holed = records(NOW, 12)
        del holed[4]
        with pytest.raises(PlanFormatError, match="inconsistent interval spacing"):
            T.translate(FakeQueryApi(holed), "planning", NOW, 27900.0)

    def test_a_renamed_field_names_itself(self):
        broken = records(NOW, 4)
        for rec in broken:
            rec.values["socWh"] = rec.values.pop("soc_wh")
        with pytest.raises(PlanFormatError, match="soc_wh"):
            T.translate(FakeQueryApi(broken), "planning", NOW, 27900.0)

    def test_the_newest_run_wins_where_horizons_overlap(self):
        old = records(NOW, 8, plan_run="2026-08-16T09:05:00Z")
        new = records(NOW, 4, plan_run="2026-08-16T12:05:00Z")
        doc, _ = T.translate(FakeQueryApi(old + new), "planning", NOW, 27900.0)
        assert doc["plan_run"] == "2026-08-16T12:05:00Z"
        assert doc["plan_runs"] == ["2026-08-16T09:05:00Z", "2026-08-16T12:05:00Z"]

    def test_the_query_window_brackets_now(self):
        q = api(NOW, 8)
        T.translate(q, "planning", NOW, 27900.0)
        assert "2026-08-16T11:07:00Z" in q.flux
        assert "2026-08-18T12:07:00Z" in q.flux
        assert 'bucket: "planning"' in q.flux


class TestAtomicWrite:
    def test_the_temp_file_is_a_sibling(self, tmp_path):
        """`Path.replace` is only atomic within a filesystem, and in the container /tmp and
        the slots volume are different mounts."""
        path = tmp_path / "nested" / "slots.json"
        T.atomic_write(path, {"slots": []})
        assert json.loads(path.read_text()) == {"slots": []}
        assert not list(path.parent.glob("*.tmp")), "temp file left behind"

    def test_a_failed_write_leaves_no_debris(self, tmp_path, monkeypatch):
        """The half-written file must not survive next to the real one. An operator finding
        `slots.json.tmp` on the volume has to be able to read it as evidence of a write
        happening now, not of one that failed at some unknown point in the past."""
        path = tmp_path / "slots.json"

        def boom(self, *a, **kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "replace", boom)
        with pytest.raises(OSError, match="no space"):
            T.atomic_write(path, {"slots": []})
        assert not list(tmp_path.glob("*.tmp")), "temp file left behind by a failed write"


class TestCapacity:
    """`BATTERY_CAPACITY_WH` is the divisor that turns the plan's Wh into a target SoC, and it
    arrives from the environment, where a typo has nothing to bounce off."""

    def test_a_plain_number_is_accepted(self):
        assert T.capacity_wh("27900") == 27900.0

    def test_a_thousands_separator_names_the_variable(self):
        """Without this the `ValueError` escapes argparse's constructor -- before logging is
        configured and before either heartbeat can fire -- as a traceback that never mentions
        which setting was wrong."""
        with pytest.raises(ValueError, match="BATTERY_CAPACITY_WH"):
            T.capacity_wh("27,900")

    @pytest.mark.parametrize("bad", ["0", "-27900"])
    def test_a_non_positive_capacity_is_refused(self, bad):
        """The dangerous one, because it parses. Zero capacity makes every SoC percentage
        either a division by zero or a number nothing downstream would question."""
        with pytest.raises(ValueError, match="positive"):
            T.capacity_wh(bad)


class Pings:
    """Collects heartbeats instead of sending them."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, url, status="up", msg="OK", timeout=5):
        self.sent.append((url, status, msg))

    def by_url(self, url) -> list[tuple[str, str]]:
        return [(s, m) for u, s, m in self.sent if u == url]


@pytest.fixture
def pings(monkeypatch) -> Pings:
    p = Pings()
    monkeypatch.setattr(T, "send_heartbeat", p)
    return p


PLAN_URL, SLOTS_URL = "http://kuma/plan", "http://kuma/slots"


def _run(query_api, path: Path, pings: Pings, **kwargs) -> int:
    return T.run(query_api, "planning", path, 27900.0, NOW,
                 plan_url=PLAN_URL, slots_url=SLOTS_URL, **kwargs)


class RecordingSlotPublisher:
    """Stands in for `slot_publisher.SlotPublisher`, and can be told to fail."""

    def __init__(self, ok=True):
        self.ok, self.docs = ok, []

    def publish(self, doc) -> bool:
        self.docs.append(doc)
        return self.ok


class TestRunPublishesTheSlots:
    """The copy Grafana can read. `slots.json` is on a volume inside the container, so
    nothing else can see what the dispatcher is about to do -- least of all when the
    container is down, which is when the question gets asked."""

    def test_a_good_run_publishes_the_document_it_wrote(self, tmp_path, pings):
        pub = RecordingSlotPublisher()
        assert _run(api(NOW, 8), tmp_path / "slots.json", pings, slot_publisher=pub) == 0
        assert len(pub.docs) == 1
        # The same object that reached the disk, not a re-derivation of it: two paths to the
        # same answer is how the file and the dashboard come to disagree.
        assert pub.docs[0] == json.loads((tmp_path / "slots.json").read_text())

    def test_a_publish_failure_does_not_fail_the_run(self, tmp_path, pings):
        """`slots.json` is the control path and this is a copy for looking at. A dashboard
        that cannot be written must not stop the battery being dispatched."""
        path = tmp_path / "slots.json"
        assert _run(api(NOW, 8), path, pings,
                    slot_publisher=RecordingSlotPublisher(ok=False)) == 0
        assert json.loads(path.read_text())["slots"]
        assert pings.by_url(SLOTS_URL)[0][0] == "up"

    def test_nothing_is_published_when_the_file_could_not_be_written(
            self, tmp_path, pings, monkeypatch):
        """Ordering, and it is deliberate. The publish sits after `atomic_write`, so a
        dashboard can never show a horizon the dispatcher never received.

        The failure is forced at `Path.replace` because `atomic_write` creates its parent
        directory -- a missing path is not a write failure here, which is worth knowing
        before writing a test that assumes it is."""
        def boom(self, *a, **kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "replace", boom)
        pub = RecordingSlotPublisher()
        assert _run(api(NOW, 8), tmp_path / "slots.json", pings, slot_publisher=pub) == 1
        assert pub.docs == []

    def test_an_unreadable_plan_publishes_nothing(self, tmp_path, pings):
        pub = RecordingSlotPublisher()
        assert _run(FakeQueryApi([], error=PlanFormatError("boom")),
                    tmp_path / "slots.json", pings, slot_publisher=pub) == 1
        assert pub.docs == []

    def test_a_dry_run_publishes_nothing(self, tmp_path, pings):
        """`--dry-run` means "write nothing", and a reader who trusted it while it quietly
        rewrote a measurement would be right to feel misled."""
        pub = RecordingSlotPublisher()
        assert _run(api(NOW, 8), tmp_path / "slots.json", pings,
                    slot_publisher=pub, dry_run=True) == 0
        assert pub.docs == []

    def test_no_publisher_is_the_normal_laptop_case(self, tmp_path, pings):
        """Optional on purpose, unlike the query api: that is the only input the control path
        has, this is observability."""
        assert _run(api(NOW, 8), tmp_path / "slots.json", pings, slot_publisher=None) == 0


class TestRun:
    def test_a_good_run_writes_the_file_and_pings_both_monitors(self, tmp_path, pings):
        path = tmp_path / "slots.json"
        assert _run(api(NOW, 8), path, pings) == 0
        assert json.loads(path.read_text())["plan_run"] == "2026-08-16T12:05:00Z"
        assert pings.by_url(PLAN_URL)[0][0] == "up"
        status, msg = pings.by_url(SLOTS_URL)[0]
        assert status == "up"
        # The message is what arrives on a phone, so it has to say what was committed to.
        assert "slots to" in msg and "2026-08-16T12:05:00Z" in msg

    def test_an_unreadable_plan_leaves_the_previous_file_alone(self, tmp_path, pings):
        """A stale slots.json is monitored and degrades gracefully. A missing one does not."""
        path = tmp_path / "slots.json"
        path.write_text('{"previous": true}')
        assert _run(FakeQueryApi([], error=PlanFormatError("boom")), path, pings) == 1
        assert json.loads(path.read_text()) == {"previous": True}

    def test_a_database_failure_still_reports_monitor_2(self, tmp_path, pings):
        """Not every read failure is a format failure. An expired token, a DNS miss or a NAS
        reboot arrives from the Influx client as an HTTP or socket error, and an uncaught one
        would end the run having pinged nothing at all -- leaving Kuma to report the silence
        as "no ping received", the one message that cannot say what broke."""
        dead = FakeQueryApi([], error=ConnectionRefusedError("[Errno 111] 192.168.68.105:8086"))
        assert _run(dead, tmp_path / "slots.json", pings) == 1
        status, msg = pings.by_url(PLAN_URL)[0]
        assert status == "down"
        assert "ConnectionRefusedError" in msg and "8086" in msg
        assert pings.by_url(SLOTS_URL) == [], "a read failure is not evidence about the writer"

    def test_a_read_failure_reports_monitor_2_and_stays_quiet_on_3(self, tmp_path, pings):
        """"The plan could not be read" is not evidence about the translator."""
        broken = records(NOW, 4)
        for rec in broken:
            del rec.values["export_wh"]
        assert _run(FakeQueryApi(broken), tmp_path / "slots.json", pings) == 1
        status, msg = pings.by_url(PLAN_URL)[0]
        assert status == "down"
        assert "export_wh" in msg, "the alert must name the field, not just say 'unreadable'"
        assert pings.by_url(SLOTS_URL) == []

    def test_a_translator_bug_reports_monitor_3_not_the_other_repo(
            self, tmp_path, pings, monkeypatch):
        """The monitors are split by WHERE THE FIX IS: #2 means look at `battery-planning`,
        #3 means look here. A plan that read cleanly and then failed in this repo's own
        arithmetic is a #3 -- reporting it as #2 sends the operator across a repo boundary to
        hunt a fault that is not there, while the monitor that means "the translation failed"
        stays green throughout."""
        def boom(*a, **kw):
            raise ZeroDivisionError("float division by zero")

        monkeypatch.setattr(T, "build_document", boom)
        assert _run(api(NOW, 8), tmp_path / "slots.json", pings) == 1
        assert pings.by_url(PLAN_URL)[0][0] == "up", "the plan itself read fine"
        status, msg = pings.by_url(SLOTS_URL)[0]
        assert status == "down"
        assert "ZeroDivisionError" in msg

    def test_a_write_failure_reports_monitor_3_with_the_plan_still_up(self, tmp_path, pings):
        # A directory where the file should be: the write fails, the read did not.
        path = tmp_path / "slots.json"
        path.mkdir()
        assert _run(api(NOW, 8), path, pings) == 1
        assert pings.by_url(PLAN_URL)[0][0] == "up"
        assert pings.by_url(SLOTS_URL)[0][0] == "down"

    def test_dry_run_writes_nothing_and_still_confirms_the_plan(self, tmp_path, pings):
        path = tmp_path / "slots.json"
        assert _run(api(NOW, 8), path, pings, dry_run=True) == 0
        assert not path.exists()
        assert pings.by_url(PLAN_URL)[0][0] == "up"
        assert pings.by_url(SLOTS_URL) == []




class TestNewestRun:
    """The tag monitor #2's "up" message names. It is the one string an operator reads on a
    phone to decide whether the battery is following a stale plan, so naming the wrong run is
    a wrong answer in the place built for right ones."""

    OFFSET = "2026-07-30T17:26:14+02:00"   # 15:26:14Z
    LATER_Z = "2026-07-30T16:00:00Z"

    def _ivs(self, *tags):
        out = []
        for i, tag in enumerate(tags):
            out += records(NOW + dt.timedelta(minutes=15 * i), 1, plan_run=tag)
        from plan import from_influx
        return from_influx(FakeQueryApi(out), "planning", NOW, NOW + dt.timedelta(hours=2))

    def test_the_later_instant_wins_over_the_later_string(self):
        assert T.newest_run(self._ivs(self.OFFSET, self.LATER_Z)) == self.LATER_Z

    def test_it_agrees_with_the_document_it_is_reported_beside(self):
        """The docstring promises the same rule `build_document` uses. If the two ever drift,
        the heartbeat names one run and slots.json records another."""
        ivs = self._ivs(self.OFFSET, self.LATER_Z)
        doc, _ = T.build_document(ivs, 27900.0, NOW)
        assert T.newest_run(ivs) == doc["plan_run"]

    def test_no_tags_at_all_is_empty_rather_than_an_error(self):
        assert T.newest_run([]) == ""
