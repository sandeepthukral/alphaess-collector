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
from state import StatePublisher, build_degraded_fields, build_fields, describe_action

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


# One decoded temperature block, as `registers.decode_temp_block` returns it: the coldest and
# hottest cell across all three packs, each tagged with the pack it came from.
TEMPS = {"min_cell_temp_c": 18.4, "min_cell_temp_pack": 1, "min_cell_temp_cell": 7,
         "max_cell_temp_c": 23.7, "max_cell_temp_pack": 3, "max_cell_temp_cell": 12}

# One decoded voltage block, as `registers.decode_voltage_block` returns it.
VOLTAGES = {"min_cell_voltage_v": 3.298, "min_cell_voltage_pack": 3, "min_cell_voltage_cell": 7,
           "max_cell_voltage_v": 3.312, "max_cell_voltage_pack": 1, "max_cell_voltage_cell": 12}


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


class TestWhatTheDispatcherKnows:
    """The four things the loop computed before touching a register, plus the verdict on its
    own write. All of it went only to the log and to Kuma until now, which is why a command
    that failed to land was invisible on every dashboard.

    The shared helper is exercised through BOTH builders on purpose: the whole point of these
    fields is that they survive an unreadable inverter, and a version that only populated the
    healthy shape would fail exactly when it was wanted.
    """

    def test_the_decision_and_the_reason_travel_with_the_readback(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, decision_kind="command",
                         reason="discharge 4500 W to 20.0%")
        assert f["decision_kind"] == "command"
        assert f["reason"] == "discharge 4500 W to 20.0%"

    def test_the_decision_and_the_reason_survive_an_unreadable_inverter(self):
        f = build_degraded_fields(read_error="timed out", decision_kind="idle",
                                  reason="live SoC unreadable")
        assert f["decision_kind"] == "idle"
        assert f["reason"] == "live SoC unreadable"

    def test_live_is_published_rather_than_inferred(self):
        """`dispatch_active == 0` is what a dry run looks like AND what a healthy release
        looks like. Section 4.1 makes the release a decision like any other, so the register
        alone cannot separate "writing nothing because we are observing" from "writing
        nothing because the plan asked for it"."""
        s, words = state_of(start=0)
        assert build_fields(s, words, NOW, decision_kind="release", live=True)["live"] == 1
        assert build_fields(s, words, NOW, decision_kind="release", live=False)["live"] == 0

    def test_the_soc_the_decision_was_made_against_is_carried(self):
        s, words = state_of()
        assert build_fields(s, words, NOW, live_soc_pct=41.2)["soc_pct"] == 41.2

    def test_an_unreadable_soc_publishes_no_soc_at_all(self):
        """The honest report of a value that was not read is no field -- not a zero, which
        would put the battery on the floor, and not the last one, which would be a guess."""
        s, words = state_of()
        assert "soc_pct" not in build_fields(s, words, NOW, live_soc_pct=None)

    def test_a_degraded_point_still_carries_the_soc_when_that_read_worked(self):
        """The commonest failure is the dispatch block alone: SoC and the block are two
        separate reads. Dropping a value that WAS obtained because a different read failed
        would throw away the one number the direction rule turned on."""
        f = build_degraded_fields(read_error="block read failed", live_soc_pct=8.4)
        assert f["soc_pct"] == 8.4

    def test_the_actual_battery_reading_is_carried(self):
        """`registers.REG_BATTERY_POWER`, charging-positive to match `setpoint_w` -- the two
        are meant to sit on the same point and be compared without a sign flip."""
        s, words = state_of()
        assert build_fields(s, words, NOW, actual_battery_w=-4453.0)["actual_battery_w"] \
            == -4453.0

    def test_an_unreadable_battery_power_publishes_no_field_at_all(self):
        """Same argument as `soc_pct`: the honest report of a value that was not read is no
        field, not a zero that would read as the battery sitting idle."""
        s, words = state_of()
        assert "actual_battery_w" not in build_fields(s, words, NOW, actual_battery_w=None)

    def test_a_degraded_point_still_carries_the_battery_reading_when_that_read_worked(self):
        """The dispatch block and the battery-power register are two separate reads; the
        commoner failure is the block alone."""
        f = build_degraded_fields(read_error="block read failed", actual_battery_w=-312.0)
        assert f["actual_battery_w"] == -312.0

    def test_the_cell_temperatures_are_carried(self):
        """`registers.TEMP_BLOCK`, decoded before the block read and published on the same
        point. Pack IDs travel with the temperatures: a hot cell is only actionable once you
        know which of the three boxes to open."""
        s, words = state_of()
        f = build_fields(s, words, NOW, temps=TEMPS)
        assert f["min_cell_temp_c"] == 18.4
        assert f["min_cell_temp_pack"] == 1
        assert f["max_cell_temp_c"] == 23.7
        assert f["max_cell_temp_pack"] == 3

    def test_the_cell_ids_are_decoded_but_not_published(self):
        """`decode_temp_block` returns them, this module drops them. The pack narrows a
        reading to a physical box; the cell within it changes nothing anyone does next, and
        every published field is a field some panel has to account for."""
        s, words = state_of()
        f = build_fields(s, words, NOW, temps=TEMPS)
        assert "min_cell_temp_cell" not in f and "max_cell_temp_cell" not in f

    def test_unread_temperatures_publish_no_fields_at_all(self):
        """Same argument as `soc_pct` and `actual_battery_w`, and sharper here: zero is a
        plausible-looking temperature, so a zero-filled field would read as a freezing
        battery rather than as a failed read."""
        s, words = state_of()
        f = build_fields(s, words, NOW, temps=None)
        assert not [k for k in f if "cell_temp" in k]

    def test_a_degraded_point_still_carries_the_temperatures_when_that_read_worked(self):
        """The temperature block and the dispatch block are separate reads, and the block is
        the commoner failure -- same as the battery-power register above."""
        f = build_degraded_fields(read_error="block read failed", temps=TEMPS)
        assert f["min_cell_temp_c"] == 18.4
        assert f["max_cell_temp_pack"] == 3

    def test_a_degraded_point_with_no_temperatures_publishes_none(self):
        assert not [k for k in build_degraded_fields(read_error="timed out", temps=None)
                    if "cell_temp" in k]

    def test_the_cell_voltages_are_carried(self):
        """`registers.VOLTAGE_BLOCK`, decoded and published the same way as the temperatures
        immediately above -- a separate read, a separate field group."""
        s, words = state_of()
        f = build_fields(s, words, NOW, voltages=VOLTAGES)
        assert f["min_cell_voltage_v"] == 3.298
        assert f["min_cell_voltage_pack"] == 3
        assert f["max_cell_voltage_v"] == 3.312
        assert f["max_cell_voltage_pack"] == 1

    def test_the_voltage_cell_ids_are_decoded_but_not_published(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, voltages=VOLTAGES)
        assert "min_cell_voltage_cell" not in f and "max_cell_voltage_cell" not in f

    def test_unread_voltages_publish_no_fields_at_all(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, voltages=None)
        assert not [k for k in f if "cell_voltage" in k]

    def test_a_degraded_point_still_carries_the_voltages_when_that_read_worked(self):
        f = build_degraded_fields(read_error="block read failed", voltages=VOLTAGES)
        assert f["min_cell_voltage_v"] == 3.298
        assert f["max_cell_voltage_pack"] == 1

    def test_a_degraded_point_with_no_voltages_publishes_none(self):
        assert not [k for k in build_degraded_fields(read_error="timed out", voltages=None)
                    if "cell_voltage" in k]

    def test_the_fault_words_are_carried(self):
        """`registers.decode_fault_block`'s output merged straight in -- every raw word,
        hex-keyed like the raw dispatch block already is. No derived count: see
        `registers.FAULT_BLOCK`'s comment on why a nonzero-word summary isn't safe yet."""
        s, words = state_of()
        faults = {"fault_raw_0131": 5, "fault_raw_0132": 0}
        f = build_fields(s, words, NOW, faults=faults)
        assert f["fault_raw_0131"] == 5
        assert f["fault_raw_0132"] == 0

    def test_unread_faults_publish_no_fields_at_all(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, faults=None)
        assert not [k for k in f if k.startswith("fault_raw_")]

    def test_a_degraded_point_still_carries_the_faults_when_that_read_worked(self):
        f = build_degraded_fields(read_error="block read failed",
                                  faults={"fault_raw_0131": 3})
        assert f["fault_raw_0131"] == 3

    def test_the_hourly_power_limits_are_carried_under_health_field_names(self):
        """Not a fresh read -- `scheduler.py` step 8c republishes `cache["limits"]` here under
        the health dashboard's own names, independent of a register having been touched this
        tick."""
        s, words = state_of()
        f = build_fields(s, words, NOW, limits_hourly=(15000, 13000))
        assert f["max_charge_power_w"] == 15000
        assert f["max_discharge_power_w"] == 13000

    def test_an_unread_limit_half_is_absent_not_zero(self):
        """`Inverter.limits()` degrades each half independently -- a `None` half must stay
        absent, not become a 0 W ceiling nobody commanded."""
        s, words = state_of()
        f = build_fields(s, words, NOW, limits_hourly=(15000, None))
        assert f["max_charge_power_w"] == 15000
        assert "max_discharge_power_w" not in f

    def test_unread_limits_publish_no_fields_at_all(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, limits_hourly=None)
        assert "max_charge_power_w" not in f and "max_discharge_power_w" not in f

    def test_the_weekly_blocks_are_carried_raw(self):
        """`registers.decode_firmware_block`/`decode_inverter_fw_block`/
        `decode_system_config_block` merged straight in, same raw-hex treatment as faults --
        which word is which named field is not confirmed, so nothing here is decoded."""
        s, words = state_of()
        f = build_fields(s, words, NOW,
                         firmware={"firmware_raw_0115": 7},
                         inverter_fw={"inverter_fw_raw_0640": 8},
                         system_config={"system_config_raw_0800": 9})
        assert f["firmware_raw_0115"] == 7
        assert f["inverter_fw_raw_0640"] == 8
        assert f["system_config_raw_0800"] == 9

    def test_unread_weekly_blocks_publish_no_fields_at_all(self):
        s, words = state_of()
        f = build_fields(s, words, NOW, firmware=None, inverter_fw=None, system_config=None)
        assert not [k for k in f if k.startswith(("firmware_raw_", "inverter_fw_raw_",
                                                    "system_config_raw_"))]

    def test_a_degraded_point_still_carries_the_weekly_blocks_when_that_read_worked(self):
        f = build_degraded_fields(read_error="block read failed",
                                  firmware={"firmware_raw_0115": 7})
        assert f["firmware_raw_0115"] == 7

    def test_a_landed_write_is_published_as_one(self):
        s, words = state_of()
        assert build_fields(s, words, NOW, write_verified=True)["verified"] == 1

    def test_a_write_that_did_not_land_is_published_as_zero(self):
        """Monitor #6's alarm, and until now the only place it existed. The log says
        commanded; the battery is not."""
        s, words = state_of()
        assert build_fields(s, words, NOW, write_verified=False)["verified"] == 0

    def test_nothing_to_verify_publishes_no_field(self):
        """THE TRI-STATE, and the reason `verified` is conditional. A release or an idle tick
        commands nothing, so a readback proves nothing -- exactly the case monitor #6 is
        documented as staying UP for. Published as 0 it would accuse a healthy dispatcher of
        a failed write on most ticks of most days."""
        s, words = state_of()
        assert "verified" not in build_fields(s, words, NOW, write_verified=None)
        assert "verified" not in build_degraded_fields(read_error="x", write_verified=None)

    def test_verified_is_an_int_so_a_stat_panel_can_threshold_it(self):
        """Grafana's stat reduces `""` by default, which is numeric fields only -- a bool
        would be dropped as silently as a string, forcing the `/.*/` path. 0/1 gets the
        threshold steps this repo already uses everywhere for red and green."""
        s, words = state_of()
        f = build_fields(s, words, NOW, write_verified=True)
        assert isinstance(f["verified"], int) and not isinstance(f["verified"], bool)

    def test_a_long_reason_is_truncated(self):
        """Free text assembled from exception strings. The same 200 the Kuma pings use, so
        the sentence does not read differently depending on where you saw it."""
        s, words = state_of()
        f = build_fields(s, words, NOW, reason="x" * 500)
        assert len(f["reason"]) == state_mod.REASON_MAX

    def test_a_bare_call_still_names_the_decision(self):
        """`reason` is unconditional, unlike everything else gated in this module, and the
        distinction is the one the module turns on: the gated fields could not be READ, while
        the loop always has a decision and always has a reason for it."""
        assert build_degraded_fields()["reason"] == "unspecified"
        assert build_degraded_fields()["decision_kind"] == "unknown"


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
