"""slots.json + now + live SoC -> a command. DESIGN-dispatch.md section 5, `dispatch/slots.py`.

Layer B of the test strategy. `decide()` is pure -- no Modbus, no clock, no filesystem -- so
every boundary instant is a table row rather than 60 seconds of waiting. That factoring is the
reason this file can exist at all, and it is worth defending: anything that needs hardware to
test belongs in scheduler.py, and anything here belongs in slots.py.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import slots as S
from registers import Command, DispatchMode

UTC = dt.UTC
T0 = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def doc(*, generated_at=None, slots=None, horizon_end=None) -> dict:
    """A minimal slots.json: discharge 12:00-12:15, hold 12:15-12:30, self 12:30-12:45."""
    return {
        "generated_at": (generated_at or T0 - dt.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "plan_run": "2026-08-01T09:00:00Z",
        "horizon_end": (horizon_end or T0 + dt.timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "interval_minutes": 15,
        "capacity_wh": 27900.0,
        "slots": slots if slots is not None else [
            {"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z",
             "action": "discharge", "power_w": 4500, "target_soc": 20.0},
            {"start": "2026-08-01T12:15:00Z", "end": "2026-08-01T12:30:00Z",
             "action": "hold"},
            {"start": "2026-08-01T12:30:00Z", "end": "2026-08-01T12:45:00Z",
             "action": "self"},
        ],
    }


class TestLoadValidation:
    def test_missing_file_says_whether_the_translator_has_run(self, tmp_path):
        with pytest.raises(S.SlotsError, match="has the translator ever run"):
            S.load(tmp_path / "nope.json")

    def test_invalid_json_carries_the_cause(self, tmp_path):
        p = tmp_path / "slots.json"
        p.write_text("{not json")
        with pytest.raises(S.SlotsError, match="not valid JSON"):
            S.load(p)

    @pytest.mark.parametrize("key", ["generated_at", "horizon_end", "slots"])
    def test_missing_top_level_key_named(self, tmp_path, key):
        d = doc()
        del d[key]
        p = tmp_path / "slots.json"
        p.write_text(json.dumps(d))
        with pytest.raises(S.SlotsError, match=key):
            S.load(p)

    def test_a_command_slot_without_power_is_rejected(self, tmp_path):
        d = doc(slots=[{"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z",
                        "action": "discharge"}])
        p = tmp_path / "slots.json"
        p.write_text(json.dumps(d))
        with pytest.raises(S.SlotsError, match="no power_w/target_soc"):
            S.load(p)

    def test_a_valid_document_round_trips(self, tmp_path):
        p = tmp_path / "slots.json"
        p.write_text(json.dumps(doc()))
        assert len(S.load(p)["slots"]) == 3


class TestFreshness:
    """Two independent ways to be stale, meaning different things: an old `generated_at`
    means the translator stopped; a passed `horizon_end` means it is fine and the plan has
    simply run out. Both end dispatch, and the operator needs to know which."""

    def test_a_fresh_plan_passes(self):
        assert S.freshness(doc(), T0) == (True, "")

    def test_an_old_plan_is_stale_and_says_its_age(self):
        old = doc(generated_at=T0 - dt.timedelta(hours=5))
        fresh, why = S.freshness(old, T0)
        assert not fresh
        assert "5.0 h old" in why

    def test_exactly_at_the_age_limit_is_still_fresh(self):
        at_limit = doc(generated_at=T0 - S.MAX_PLAN_AGE)
        assert S.freshness(at_limit, T0)[0]

    def test_a_passed_horizon_is_stale_for_a_different_reason(self):
        fresh, why = S.freshness(doc(), T0 + dt.timedelta(hours=1))
        assert not fresh
        assert "horizon ended" in why

    def test_the_horizon_is_half_open(self):
        d = doc()
        assert not S.freshness(d, dt.datetime(2026, 8, 1, 12, 45, tzinfo=UTC))[0]
        assert S.freshness(d, dt.datetime(2026, 8, 1, 12, 44, 59, tzinfo=UTC))[0]

    def test_a_future_plan_is_rejected_as_clock_skew(self):
        """The translator and the dispatcher must agree on time -- every slot decision
        depends on it, so a skewed clock would mis-select slots rather than fail."""
        future = doc(generated_at=T0 + dt.timedelta(minutes=30))
        fresh, why = S.freshness(future, T0)
        assert not fresh
        assert "clock skew" in why

    def test_ordinary_ntp_drift_is_tolerated(self):
        near = doc(generated_at=T0 + dt.timedelta(minutes=2))
        assert S.freshness(near, T0)[0]

    def test_unparseable_timestamp_raises(self):
        with pytest.raises(S.SlotsError, match="unparseable timestamp"):
            S.freshness({"generated_at": "yesterday", "horizon_end": "x"}, T0)


class TestFindSlot:
    @pytest.mark.parametrize("minute,expected", [
        (0, "discharge"),      # exactly at the start -- inclusive
        (7, "discharge"),      # mid-slot
        (14, "discharge"),
        (15, "hold"),          # exactly at the boundary -- belongs to the NEXT slot
        (30, "self"),
        (44, "self"),
    ])
    def test_half_open_intervals(self, minute, expected):
        found = S.find_slot(doc(), T0 + dt.timedelta(minutes=minute))
        assert found["action"] == expected

    def test_before_the_first_slot_is_no_slot(self):
        assert S.find_slot(doc(), T0 - dt.timedelta(minutes=1)) is None

    def test_at_the_horizon_end_is_no_slot(self):
        assert S.find_slot(doc(), T0 + dt.timedelta(minutes=45)) is None

    def test_a_gap_between_slots_is_no_slot(self):
        d = doc(slots=[
            {"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z", "action": "hold"},
            {"start": "2026-08-01T12:30:00Z", "end": "2026-08-01T12:45:00Z", "action": "hold"},
        ])
        assert S.find_slot(d, T0 + dt.timedelta(minutes=20)) is None

    def test_overlaps_resolve_to_the_earliest_deterministically(self):
        """Section 4.3 makes overlap a warning, not an error -- the translator should not
        produce one, but refusing to dispatch at all would be a worse failure than picking
        deterministically."""
        d = doc(slots=[
            {"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:30:00Z", "action": "hold"},
            {"start": "2026-08-01T12:10:00Z", "end": "2026-08-01T12:40:00Z", "action": "self"},
        ])
        assert S.find_slot(d, T0 + dt.timedelta(minutes=15))["action"] == "hold"


class TestDecide:
    def test_no_document_is_idle_and_carries_the_load_error(self):
        d = S.decide(None, T0, 50.0, "slots.json vanished")
        assert d.kind == "idle"
        assert d.reason == "slots.json vanished"
        assert not d.fresh

    def test_a_stale_plan_is_idle_not_a_release(self):
        """Idle writes NOTHING. Section 1's fail-safe is silence -- the inverter's own dead
        man's switch reverts it without our help."""
        assert S.decide(doc(generated_at=T0 - dt.timedelta(hours=9)), T0, 50.0).kind == "idle"

    def test_a_gap_is_idle(self):
        assert S.decide(doc(), T0 - dt.timedelta(minutes=1), 50.0).kind == "idle"

    def test_self_is_a_release_not_an_idle(self):
        """`self` is a real decision -- the plan asked for self-consumption NOW, not in up to
        five minutes when the duration expires."""
        d = S.decide(doc(), T0 + dt.timedelta(minutes=30), 50.0)
        assert d.kind == "release"
        assert not d.dispatching

    def test_hold_commands_mode_3_at_zero_watts(self):
        d = S.decide(doc(), T0 + dt.timedelta(minutes=15), 50.0)
        assert d.kind == "command"
        assert d.command.mode == DispatchMode.FOLLOW
        assert d.command.power_w == 0
        assert d.command.target_soc_pct is None

    def test_a_discharge_becomes_a_signed_mode_2_command(self):
        d = S.decide(doc(), T0, 80.0)
        assert d.command.mode == DispatchMode.SOC_TARGET
        assert d.command.power_w == -4500        # discharging is negative in our convention
        assert d.command.target_soc_pct == 20.0
        assert d.command.duration_s == S.DISPATCH_DURATION_S

    def test_a_charge_stays_positive(self):
        d = doc(slots=[{"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z",
                        "action": "charge", "power_w": 4000, "target_soc": 90.0}])
        assert S.decide(d, T0, 50.0).command.power_w == 4000

    def test_unreadable_soc_is_idle_because_the_direction_rule_cannot_be_checked(self):
        d = S.decide(doc(), T0, None)
        assert d.kind == "idle"
        assert "direction rule" in d.reason

    def test_the_slot_travels_with_the_decision(self):
        """The heartbeat records it, so `--alive` can say what the loop thinks it is doing."""
        assert S.decide(doc(), T0, 80.0).slot["action"] == "discharge"


class TestDirectionRuleAgainstLiveSoc:
    """The plan's trajectory was a forecast made up to three hours ago; the battery is where
    it actually is."""

    def test_a_discharge_below_live_soc_proceeds(self):
        assert S.decide(doc(), T0, 80.0).command.mode == DispatchMode.SOC_TARGET

    def test_a_discharge_target_above_live_soc_holds_instead(self):
        d = S.decide(doc(), T0, 19.0)
        assert d.command.mode == DispatchMode.FOLLOW
        assert "not below live SoC" in d.reason

    def test_the_deadband_catches_a_target_within_a_hair(self):
        """The SoC register steps in 0.4 % and the measurement reads to 0.1 %, so a target
        within a hair of live SoC is a command to do nothing -- which the inverter accepts,
        leaving every monitor green while nothing happens."""
        d = S.decide(doc(), T0, 20.5)
        assert d.command.mode == DispatchMode.FOLLOW

    def test_just_outside_the_deadband_proceeds(self):
        assert S.decide(doc(), T0, 21.5).command.mode == DispatchMode.SOC_TARGET

    def test_a_charge_target_below_live_soc_holds_instead(self):
        d = doc(slots=[{"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z",
                        "action": "charge", "power_w": 4000, "target_soc": 60.0}])
        decision = S.decide(d, T0, 62.0)
        assert decision.command.mode == DispatchMode.FOLLOW
        assert "not above live SoC" in decision.reason


class TestClamp:
    """The ceiling is the LOWER of what the inverter reports (0x012C/0x012D) and
    `HARD_MAX_POWER_W`. The hardware overstates itself -- 15,015 W charge / 13,728 W discharge,
    measured 2026-08-16 on a 5 kW unit -- so a clamp trusting it alone stops nothing. A clamp
    firing means the plan asked for something it never should have."""

    def test_an_in_range_command_is_untouched(self):
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        out, warn = S.clamp(cmd, 5000, 5000)
        assert out is cmd and warn == ""

    def test_an_excessive_discharge_is_clamped_loudly(self):
        out, warn = S.clamp(Command(DispatchMode.SOC_TARGET, -9000, 20.0, 300), 5000, 5000)
        assert out.power_w == -5000
        assert "exceeds the 5000 W ceiling" in warn

    def test_an_excessive_charge_is_clamped_loudly(self):
        out, warn = S.clamp(Command(DispatchMode.SOC_TARGET, 9000, 90.0, 300), 5000, 5000)
        assert out.power_w == 5000
        assert "exceeds the 5000 W ceiling" in warn

    def test_the_registers_own_generous_limits_do_not_raise_the_ceiling(self):
        """The real numbers off the real inverter. Trusting them would let a 9 kW command
        through to a 5 kW machine."""
        out, warn = S.clamp(Command(DispatchMode.SOC_TARGET, 9000, 90.0, 300), 15015, 13728)
        assert out.power_w == S.HARD_MAX_POWER_W
        assert warn

    def test_a_lower_reported_limit_still_wins(self):
        """A derate, a firmware change, a different unit -- the hardware is still in the loop
        when it reports something SMALLER than expected."""
        out, warn = S.clamp(Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300), 3000, 3000)
        assert out.power_w == -3000
        assert "3000 W ceiling" in warn

    def test_unknown_limits_fall_back_to_the_hard_ceiling(self):
        """Losing the register read must not lose the ceiling with it. The translator should
        already prevent an out-of-range command; this is the layer that assumes it did not."""
        cmd = Command(DispatchMode.SOC_TARGET, -9000, 20.0, 300)
        out, warn = S.clamp(cmd, None, None)
        assert out.power_w == -S.HARD_MAX_POWER_W
        assert warn

    def test_an_in_range_command_survives_unknown_limits(self):
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert S.clamp(cmd, None, None)[0] is cmd

    def test_clamping_preserves_mode_target_and_duration(self):
        out, _ = S.clamp(Command(DispatchMode.SOC_TARGET, -9000, 20.0, 300), 5000, 5000)
        assert (out.mode, out.target_soc_pct, out.duration_s) == (2, 20.0, 300)


class TestIsHijacked:
    """Section 5 step 5. The AlphaESS app writes these same registers -- caught on
    2026-08-15 16:11 holding a 93-minute grid force-charge."""

    def state(self, **over):
        base = {"dispatch_active": 1, "mode": 2, "power_w": -4500,
                "target_soc_pct": 20.0, "duration_s": 300}
        return {**base, **over}

    def test_an_inactive_block_is_never_a_hijack(self):
        assert not S.is_hijacked(self.state(dispatch_active=0), None)

    def test_an_active_block_we_did_not_write_is_a_hijack(self):
        assert S.is_hijacked(self.state(), None)

    def test_our_own_command_read_back_is_not_a_hijack(self):
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert not S.is_hijacked(self.state(), cmd)

    def test_the_apps_force_charge_signature_is_caught(self):
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert S.is_hijacked(
            self.state(mode=2, power_w=5000, target_soc_pct=100.0, duration_s=5580), cmd)

    def test_a_changed_mode_is_a_hijack(self):
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert S.is_hijacked(self.state(mode=3), cmd)

    def test_duration_is_deliberately_not_compared(self):
        """It counts down, and section 5.1 records that it does so erratically -- observed
        reading 300 s three times across two minutes, then straight to expiry."""
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert not S.is_hijacked(self.state(duration_s=112), cmd)

    def test_a_hold_ignores_the_stale_soc_register(self):
        """A Mode 3 hold writes no SoC target, so whatever 0x0886 still holds is not
        evidence of anything."""
        cmd = Command(DispatchMode.FOLLOW, 0, None, 300)
        assert not S.is_hijacked(
            self.state(mode=3, power_w=0, target_soc_pct=77.6), cmd)

    def test_soc_within_one_register_step_is_not_a_hijack(self):
        """0.4 %/bit -- a readback cannot be more precise than the step it was written at."""
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert not S.is_hijacked(self.state(target_soc_pct=20.4), cmd)


class TestFailsafeConstants:
    def test_the_duration_is_a_multiple_of_the_refresh_interval(self):
        """The GAP between them is the failsafe margin -- how many ticks may be missed before
        the inverter reverts on its own -- not a knob to shrink for its own sake."""
        assert S.DISPATCH_DURATION_S > S.REFRESH_INTERVAL_S
        missable = S.DISPATCH_DURATION_S // S.REFRESH_INTERVAL_S - 1
        assert missable >= 3, "fewer than three missable ticks leaves no margin"
