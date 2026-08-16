"""Publishing the command to InfluxDB. DESIGN-dispatch.md section 7.1, `dispatch/state.py`.

The panels this feeds exist to answer a question no register can: `start=0` is a legitimate
commanded state, so at the register level "deliberately running self-consumption" and
"crashed an hour ago" look identical. What separates them is the DECISION, which only the
dispatcher knows -- which is why it is published rather than inferred in Flux.
"""
from __future__ import annotations

import datetime as dt

import pytest

import registers as R
import state as state_mod
from state import StatePublisher, build_fields, describe_action

UTC = dt.UTC
NOW = dt.datetime(2026, 8, 15, 18, 30, tzinfo=UTC)


def block(*, start=1, power_w=-4500, soc_pct=20.0, mode=2, duration=300) -> list[int]:
    words = [0] * 9
    words[0] = start
    words[1:3] = R.encode_power(power_w)
    words[5] = mode
    words[6] = R.encode_soc(soc_pct)[0]
    words[7:9] = R.encode_int32(duration)
    return words


def state_of(**kw) -> tuple[dict, list[int]]:
    words = block(**kw)
    return R.decode_block(words), words


class TestDescribeAction:
    """The field the dashboard leads with. It says what is HAPPENING, not which mode is set:
    "charging from grid" answers the question being asked; "mode 2" is a protocol fact."""

    def test_a_grid_charge(self):
        s, _ = state_of(power_w=4000, mode=2)
        assert describe_action(s) == "charging from grid"

    def test_a_grid_discharge(self):
        s, _ = state_of(power_w=-4500, mode=2)
        assert describe_action(s) == "discharging to grid"

    def test_a_hold_says_the_battery_is_frozen(self):
        s, _ = state_of(power_w=0, mode=3)
        assert describe_action(s) == "hold (battery frozen)"

    def test_a_pv_charge_is_distinguished_from_a_grid_charge(self):
        s, _ = state_of(power_w=3000, mode=1)
        assert describe_action(s) == "charging from PV"

    def test_a_release_is_named_as_a_decision(self):
        """`start=0` because the plan asked for self-consumption."""
        s, _ = state_of(start=0)
        assert describe_action(s, "release") == "self-consumption (released)"

    def test_an_inactive_block_without_a_release_is_not_claimed_as_one(self):
        """The dispatcher may be idle, or dead. Do not assert a decision that was not made.

        This is the distinction that makes the panel honest: reading `start=0` alone can
        never tell those apart, so the decision has to travel with the data.
        """
        s, _ = state_of(start=0)
        assert describe_action(s, "idle") == "no dispatch"
        assert describe_action(s, "") == "no dispatch"


class TestBuildFields:
    def test_the_decoded_fields_match_the_contract(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, decision_kind="command")
        assert f["dispatch_active"] == 1
        assert f["mode"] == 2
        assert f["mode_name"] == "SoC control"
        assert f["action"] == "discharging to grid"
        assert f["setpoint_w"] == -4500
        assert f["target_soc_pct"] == 20.0
        assert f["duration_s"] == 300

    def test_setpoint_is_charging_positive(self):
        """Every other panel on this dashboard counts charging positive -- panel 9 negates
        `battery_power_w` in Flux for exactly this reason. A "Commanded" stat disagreeing in
        sign with the "Battery Power now" stat beside it would be worse than no panel."""
        s, words = state_of(power_w=4000)
        assert build_fields(s, words, NOW)["setpoint_w"] == 4000

    def test_the_raw_block_is_published_verbatim(self):
        """For the morning after, when a decode turns out to have been wrong. At one point
        per minute this costs nothing and is the only way to re-derive history."""
        s, words = state_of()
        f = build_fields(s, words, NOW)
        assert f["raw_0880"] == 1
        assert f["raw_0885"] == 2
        assert f["raw_0886"] == 50
        assert [f[f"raw_{0x0880 + i:04x}"] for i in range(9)] == words

    def test_raw_fields_are_named_by_hex_address(self):
        """Matching the spec PDFs and the dashboard decode table -- the wire is indexed in
        decimal and the documentation in hex, and that translation is where errors live."""
        s, words = state_of()
        f = build_fields(s, words, NOW)
        assert "raw_0885" in f and "raw_2181" not in f

    def test_expires_at_is_write_time_plus_duration(self):
        """Derived rather than read from the countdown register, which section 5.1 records
        counting down erratically."""
        s, words = state_of(duration=300)
        assert build_fields(s, words, NOW)["expires_at"] == int(NOW.timestamp()) + 300

    def test_an_inactive_block_has_no_expiry(self):
        s, words = state_of(start=0)
        assert "expires_at" not in build_fields(s, words, NOW)

    def test_the_slot_and_plan_run_travel_with_the_command(self):
        """Without these the dashboard shows a command; with them it shows WHICH PLAN asked
        for it -- the difference between "charging at 15 ct" and "charging at 15 ct because
        it is still serving a plan from six hours ago"."""
        s, words = state_of()
        f = build_fields(
            s, words, NOW, decision_kind="command",
            slot={"start": "2026-08-15T18:15:00Z", "end": "2026-08-15T18:30:00Z",
                  "action": "discharge"},
            plan_run="2026-08-15T15:00:00Z")
        assert f["slot_start"] == int(
            dt.datetime(2026, 8, 15, 18, 15, tzinfo=UTC).timestamp())
        assert f["slot_action"] == "discharge"
        assert f["plan_run"] == "2026-08-15T15:00:00Z"

    def test_slot_fields_are_omitted_when_there_is_no_slot(self):
        s, words = state_of(start=0)
        f = build_fields(s, words, NOW, decision_kind="idle")
        assert "slot_start" not in f and "plan_run" not in f

    def test_a_short_block_is_rejected(self):
        s, _ = state_of()
        with pytest.raises(ValueError, match="expected 9 raw words"):
            build_fields(s, [0] * 8, NOW)

    def test_every_field_is_an_influx_scalar(self):
        """Point.field() takes int/float/str/bool. A stray tuple or None serialises into a
        line-protocol error at write time, which the publisher then swallows."""
        s, words = state_of()
        f = build_fields(s, words, NOW, decision_kind="command",
                         slot={"start": "2026-08-15T18:15:00Z", "action": "discharge"},
                         plan_run="r")
        for key, value in f.items():
            assert isinstance(value, int | float | str), (key, type(value))


class FakeWriteApi:
    def __init__(self, fail_times=0):
        self.written, self.fail_times = [], fail_times

    def write(self, bucket, record):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("influx is down")
        self.written.append((bucket, record))


class TestStatePublisher:
    def test_a_point_reaches_the_right_bucket(self):
        api = FakeWriteApi()
        s, words = state_of()
        assert StatePublisher(api, "alphaess", "SN1").publish(build_fields(s, words, NOW))
        bucket, record = api.written[0]
        assert bucket == "alphaess"
        line = record.to_line_protocol()
        assert line.startswith(state_mod.MEASUREMENT)
        assert "sys_sn=SN1" in line

    def test_a_write_failure_never_raises(self):
        """Bookkeeping about the control loop must not be able to stop it."""
        api = FakeWriteApi(fail_times=1)
        s, words = state_of()
        assert StatePublisher(api, "alphaess").publish(build_fields(s, words, NOW)) is False

    def test_repeated_failures_log_once_then_recover(self, caplog):
        """At 60 s a broken InfluxDB would otherwise produce 1,440 identical lines a day and
        bury the thing that actually matters."""
        api = FakeWriteApi(fail_times=3)
        pub = StatePublisher(api, "alphaess")
        s, words = state_of()
        fields = build_fields(s, words, NOW)
        with caplog.at_level("INFO", logger="dispatch"):
            for _ in range(3):
                pub.publish(fields)
            assert sum("write failed" in r.message for r in caplog.records) == 1
            pub.publish(fields)
            assert any("recovered" in r.message for r in caplog.records)

    def test_no_write_api_is_a_no_op(self):
        """A laptop dry run needs no token, and losing Influx must degrade the dashboard
        rather than the control loop."""
        s, words = state_of()
        assert StatePublisher(None, "alphaess").publish(build_fields(s, words, NOW)) is False
