"""`dispatch_slots` -- the translator's own output, where something else can read it.

`slots.json` is on a named volume inside the container, so Grafana cannot see it and neither
can you when the container is down -- which is when "what would it be doing now" is worth
asking. These tests pin the shape of the copy that goes to InfluxDB instead.
"""
from __future__ import annotations

import datetime as dt

import pytest

import slot_publisher as SP

UTC = dt.UTC


def doc(*slots, plan_run="2026-08-16T12:05:00Z") -> dict:
    return {"generated_at": "2026-08-16T12:10:00Z", "plan_run": plan_run,
            "plan_runs": [plan_run], "horizon_end": "2026-08-17T00:00:00Z",
            "interval_minutes": 15, "capacity_wh": 27900.0, "slots": list(slots)}


CHARGE = {"start": "2026-08-16T12:15:00Z", "end": "2026-08-16T13:15:00Z",
          "action": "charge", "power_w": 4629, "target_soc": 82.0}
HOLD = {"start": "2026-08-16T13:15:00Z", "end": "2026-08-16T14:00:00Z", "action": "hold"}


class TestBuildRecords:
    def test_one_record_per_slot(self):
        assert len(SP.build_records(doc(CHARGE, HOLD))) == 2

    def test_a_record_is_stamped_at_the_slot_start(self):
        """These are points about time that has not happened yet, exactly like the `plan`
        measurement they derive from -- which is why every dashboard query against that
        bucket carries a `stop:` in the future. A reader who forgets gets an empty table."""
        rec = SP.build_records(doc(CHARGE))[0]
        assert rec["time"] == dt.datetime(2026, 8, 16, 12, 15, tzinfo=UTC)

    def test_the_end_is_a_field_not_a_second_point(self):
        """A slot is one thing with two edges. Splitting it into a start row and an end row
        would make every consumer re-pair them."""
        f = SP.build_records(doc(CHARGE))[0]["fields"]
        assert f["end_s"] == int(dt.datetime(2026, 8, 16, 13, 15, tzinfo=UTC).timestamp())
        assert f["duration_s"] == 3600

    def test_a_charge_carries_its_power_and_target(self):
        f = SP.build_records(doc(CHARGE))[0]["fields"]
        assert f["action"] == "charge"
        assert f["power_w"] == 4629
        assert f["target_soc"] == 82.0

    def test_a_hold_carries_neither(self):
        """Mirrors `Slot.__post_init__`, which makes a hold carrying power_w an error rather
        than a curiosity. Zeros would read as "charge at 0 W to 0 %" -- a command this
        dispatcher can actually issue, and therefore genuinely ambiguous to write down."""
        f = SP.build_records(doc(HOLD))[0]["fields"]
        assert f["action"] == "hold"
        assert "power_w" not in f and "target_soc" not in f

    def test_every_record_is_tagged_with_the_plan_run(self):
        """What lets a query pick one run's slots and not a mixture. Boundaries move between
        runs, so an untagged write would strand an older run's slots at timestamps the newer
        one does not cover: visible, plausible, and wrong."""
        for rec in SP.build_records(doc(CHARGE, HOLD)):
            assert rec["tags"]["plan_run"] == "2026-08-16T12:05:00Z"

    def test_the_serial_is_tagged_when_there_is_one(self):
        assert SP.build_records(doc(CHARGE), sys_sn="AL123")[0]["tags"]["sys_sn"] == "AL123"

    def test_no_serial_means_no_empty_tag(self):
        """An empty tag value is not the same as an absent tag, and Influx keeps both."""
        assert "sys_sn" not in SP.build_records(doc(CHARGE))[0]["tags"]

    def test_records_do_not_share_a_tag_dict(self):
        """Each record carries its own copy, so a caller mutating one cannot retag the rest.
        A shared dict here would be invisible until something did."""
        recs = SP.build_records(doc(CHARGE, HOLD))
        recs[0]["tags"]["plan_run"] = "tampered"
        assert recs[1]["tags"]["plan_run"] == "2026-08-16T12:05:00Z"

    def test_an_empty_document_yields_nothing(self):
        assert SP.build_records(doc()) == []

    def test_every_field_is_an_influx_scalar(self):
        for rec in SP.build_records(doc(CHARGE, HOLD)):
            for key, value in rec["fields"].items():
                assert isinstance(value, int | float | str), (key, type(value))

    def test_republishing_an_unchanged_plan_repeats_itself_exactly(self):
        """WHY RE-PUBLISHING IS SAFE. The translator runs every five minutes (#107) while the
        planner runs hourly, so most passes re-translate a plan that has not changed. Same
        tag, same timestamps, same values means Influx overwrites in place instead of
        accumulating twelve copies an hour."""
        assert SP.build_records(doc(CHARGE, HOLD)) == SP.build_records(doc(CHARGE, HOLD))


class FakeWriteApi:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def write(self, bucket, record):
        if self.fail:
            raise RuntimeError("influx is down")
        self.calls.append((bucket, record))


class TestSlotPublisher:
    def test_the_whole_horizon_goes_in_one_call(self):
        """Fifty synchronous round trips would make a failure partial, and a partly-written
        horizon is worse than an unwritten one: it renders as a plan with a hole in it rather
        than as a plan that is missing."""
        api = FakeWriteApi()
        assert SP.SlotPublisher(api, "alphaess").publish(doc(CHARGE, HOLD)) is True
        assert len(api.calls) == 1
        bucket, record = api.calls[0]
        assert bucket == "alphaess"
        assert len(record) == 2

    def test_a_write_failure_never_raises(self):
        """`slots.json` is what the dispatcher acts on; this is a copy for looking at, and a
        copy that could break the translator would be worth less than no copy."""
        assert SP.SlotPublisher(FakeWriteApi(fail=True), "alphaess").publish(doc(CHARGE)) is False

    def test_repeated_failures_log_once_then_recover(self, caplog):
        api = FakeWriteApi(fail=True)
        pub = SP.SlotPublisher(api, "alphaess")
        with caplog.at_level("INFO"):
            pub.publish(doc(CHARGE))
            pub.publish(doc(CHARGE))
            api.fail = False
            pub.publish(doc(CHARGE))
        assert sum("write failed" in r.message for r in caplog.records) == 1
        assert sum("recovered" in r.message for r in caplog.records) == 1

    def test_no_write_api_is_a_no_op(self):
        assert SP.SlotPublisher(None, "alphaess").publish(doc(CHARGE)) is False

    def test_an_empty_document_writes_nothing(self):
        """Not an error, and not a write of zero points either: a horizon that translated to
        no slots is a fault the translator itself reports, and a bucket write here would only
        add noise to it."""
        api = FakeWriteApi()
        assert SP.SlotPublisher(api, "alphaess").publish(doc()) is False
        assert api.calls == []


class TestMeasurementName:
    def test_it_is_not_the_dispatch_state_measurement(self):
        """Two measurements, two lifetimes: `dispatch_state` is one point a minute about now,
        this is a horizon rewritten every five minutes about the future. Sharing a name would
        make every `-5m` staleness guard in the dashboard read future points as current."""
        import state

        assert SP.MEASUREMENT != state.MEASUREMENT


@pytest.mark.parametrize("iso,expected", [
    ("2026-08-16T12:15:00Z", dt.datetime(2026, 8, 16, 12, 15, tzinfo=UTC)),
    ("2026-08-16T14:15:00+02:00", dt.datetime(2026, 8, 16, 12, 15, tzinfo=UTC)),
])
def test_both_offset_spellings_parse_to_the_same_instant(iso, expected):
    """Runs written before 2026-07-30 carry `+02:00` where later ones carry `Z`. Never a
    string compare -- see `plan.run_sort_key`."""
    slot = {"start": iso, "end": "2026-08-16T15:00:00Z", "action": "hold"}
    assert SP.build_records(doc(slot))[0]["time"] == expected
