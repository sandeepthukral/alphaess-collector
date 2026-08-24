"""Plan intervals -> dispatch slots. DESIGN-dispatch.md section 4, `dispatch/translator.py`.

The judgement being tested is section 4.1's, and it looks like an omission until stated:

    Where the plan is SPECIFIC about wattage, forced dispatch is right. Where the plan is
    INDIFFERENT to it -- cover the house load, absorb whatever surplus exists -- the right
    command is NO command.

So `self` is a real decision here, not a gap, and most of these tests exist to keep it one.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from plan import PlanFormatError, PlanInterval, from_table
from translator import (
    ENERGY_FLOOR_WH,
    FULL_TOLERANCE_WH,
    SURPLUS_FLOOR_WH,
    Slot,
    build_document,
    classify,
    to_slots,
)

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
UTC = dt.UTC
CAPACITY = 27900.0


def iv(*, charge=0.0, discharge=0.0, imp=0.0, exp=0.0, soc=14000.0,
       pv=0.0, minute=0, run="2026-08-01T09:00:00Z",
       discharge_power_w=None) -> PlanInterval:
    return PlanInterval(
        start=dt.datetime(2026, 8, 1, 12, minute, tzinfo=UTC),
        soc_wh=soc, charge_wh=charge, discharge_wh=discharge,
        import_wh=imp, export_wh=exp, plan_run=run, pv_forecast_wh=pv,
        discharge_power_w=discharge_power_w)


class TestClassify:
    """Section 4.1's table. The distinction is not charge-vs-discharge, it is whether the
    energy CROSSES THE METER: discharging into the house and discharging to the grid are the
    same battery behaviour at different prices, and only the second needs commanding."""

    def test_discharge_to_the_grid_is_a_command(self):
        assert classify(iv(discharge=1000, exp=900)) == "discharge"

    def test_discharge_into_the_house_is_self(self):
        assert classify(iv(discharge=1000, exp=0)) == "self"

    def test_charge_from_the_grid_is_a_command(self):
        assert classify(iv(charge=1000, imp=900)) == "charge"

    def test_charge_from_surplus_pv_is_self(self):
        """The expensive case. Commanding a forced charge here would pull the shortfall from
        the grid whenever solar underdelivers -- importing energy the plan priced at zero.
        Solar underdelivering against forecast is the normal case, not an edge case."""
        assert classify(iv(charge=1000, imp=0)) == "self"

    def test_an_idle_interval_holds(self):
        assert classify(iv()) == "hold"

    @pytest.mark.parametrize("wh", [0.0, 1.0, ENERGY_FLOOR_WH])
    def test_energy_at_or_below_the_floor_is_a_rounding_artefact(self, wh):
        # advise.py uses 1 Wh, which is right for a human-readable report and far too low
        # here: at 15-minute intervals 1 Wh is 4 W of dispatch.
        assert classify(iv(discharge=wh, exp=wh)) == "hold"

    def test_just_above_the_floor_is_deliberate(self):
        assert classify(iv(discharge=ENERGY_FLOOR_WH + 0.1, exp=1000)) == "discharge"

    def test_export_below_the_floor_does_not_make_it_a_command(self):
        assert classify(iv(discharge=1000, exp=ENERGY_FLOOR_WH)) == "self"

    def test_charge_and_discharge_together_raises(self):
        """The LP should never emit both -- it would pay round-trip losses to stand still.

        Dead code against the real archive (0 of ~13,000 intervals), so it needs a
        hand-written case or the branch is untested.
        """
        with pytest.raises(PlanFormatError, match=r"both charges .* and discharges"):
            classify(iv(charge=1000, discharge=1000, imp=900, exp=900))


class TestPowerAndTarget:
    def test_power_is_scaled_to_the_cadence(self):
        """900 Wh over a quarter hour is 3600 W, not 900."""
        slots, _ = to_slots([iv(charge=900, imp=900, soc=15000, minute=0),
                             iv(charge=900, imp=900, soc=16000, minute=15)], CAPACITY)
        assert slots[0].power_w == 3600

    def test_target_is_this_intervals_own_soc(self):
        """Section 3.2. `soc_wh` is the END of the interval, so the target is its own value.

        Taking the next point's shifts every target one interval late -- a plausible-looking
        plan that consistently underdelivers.
        """
        slots, _ = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0),
                             iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].target_soc == 50.0        # 13950 / 27900

    def test_discharge_power_w_overrides_the_derived_setpoint(self):
        """DISPATCH-FLOW.md's discharge-ceiling override: when the plan supplies
        discharge_power_w, that goes on the wire instead of discharge_wh * 60/minutes --
        Marstek-planning.py sets it above what discharge_wh implies specifically so the
        wire command clears the inverter's regulation floor."""
        slots, _ = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0,
                                discharge_power_w=5000),
                             iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].power_w == 5000            # not round(900 * 4) == 3600
        assert slots[0].target_soc == 50.0          # soc_wh untouched by the override

    def test_discharge_power_w_is_ignored_when_absent(self):
        """The common case -- and the charge case, which never carries this field --
        is untouched: derive power_w from discharge_wh/charge_wh exactly as before."""
        slots, _ = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0),
                             iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].power_w == 3600             # round(900 * 4), unchanged

    def test_discharge_power_w_is_inert_on_a_charge_interval(self):
        """discharge_power_w should never coexist with a real charge interval -- the planner
        only sets it on discharge intervals -- but the override is gated on
        `action == "discharge"`, not merely "the field is set", specifically so a stray or
        malformed value on a charge interval cannot do anything."""
        slots, _ = to_slots([iv(charge=900, imp=900, soc=15000, minute=0,
                                discharge_power_w=5000),
                             iv(charge=900, imp=900, soc=16000, minute=15)], CAPACITY)
        assert slots[0].action == "charge"
        assert slots[0].power_w == 3600            # round(900 * 4) -- the override is ignored

    def test_discharge_power_w_of_zero_is_downgraded_like_any_other_zero(self):
        """Boundary: 0.0 is not None, so it reaches the override branch and becomes
        power_w == 0 -- which the existing rounds-to-zero guard downgrades to hold, the same
        as a discharge_wh so small it rounds to 0 W."""
        slots, warnings = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0,
                                       discharge_power_w=0.0),
                                    iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].action == "hold"
        assert slots[0].power_w is None
        assert "setpoint override of 0 W" in warnings[0]

    def test_a_negative_discharge_power_w_is_downgraded_not_sign_flipped(self):
        """The guard that actually matters: slots.py:decide() negates a discharge slot's
        power_w to get the wire's charging-positive Command -- `power = -int(slot["power_w"])`.
        A negative override reaching that line unchecked would silently become a POSITIVE
        (charging) command during what the plan intended as a discharge. The rounds-to-zero-
        or-below guard (power_w <= 0) catches this too, before it ever becomes a Slot."""
        slots, warnings = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0,
                                       discharge_power_w=-100.0),
                                    iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].action == "hold"
        assert slots[0].power_w is None
        assert "setpoint override of -100 W" in warnings[0]

    def test_an_oversized_discharge_power_w_passes_through_to_slots_uncapped(self):
        """to_slots() does not itself bound the magnitude -- that is slots.py's clamp()
        (HARD_MAX_POWER_W), exercised end-to-end in test_dispatch_slots.py's TestClamp,
        which does not care whether a Command's power_w originated from discharge_wh or from
        this override. This just documents that to_slots() passes an oversized override
        through rather than silently dropping or truncating it -- clamp() is the one place
        that bound is enforced, and it is enforced on every Command regardless of origin."""
        slots, warnings = to_slots([iv(discharge=900, exp=900, soc=13950, minute=0,
                                       discharge_power_w=50000.0),
                                    iv(discharge=900, exp=900, soc=13000, minute=15)], CAPACITY)
        assert slots[0].action == "discharge"
        assert slots[0].power_w == 50000
        assert warnings == []

    def test_discharge_power_w_does_not_leak_into_a_downgraded_slot(self):
        """A discharge whose target doesn't actually lower SoC is downgraded to hold
        (TestDirectionRule) regardless of what discharge_power_w says -- the override
        must not resurrect a power_w the direction rule already refused."""
        slots, warnings = to_slots([
            iv(soc=14000, minute=0),
            iv(discharge=900, exp=900, soc=15000, minute=15, discharge_power_w=5000),
        ], CAPACITY)
        assert [s.action for s in slots] == ["hold"]
        assert slots[0].power_w is None
        assert "not below" in warnings[0]


class TestDirectionRule:
    """A Mode 2 command whose target sits the wrong side of the current SoC is a silent
    no-op: accepted by the inverter, ignored, every monitor green."""

    def test_a_discharge_that_does_not_lower_soc_is_downgraded(self):
        slots, warnings = to_slots([
            iv(soc=14000, minute=0),
            iv(discharge=900, exp=900, soc=15000, minute=15),
        ], CAPACITY)
        # Downgraded to hold, which then merges with the preceding hold -- one slot, not two.
        assert [s.action for s in slots] == ["hold"]
        assert slots[0].power_w is None
        assert "not below" in warnings[0]

    def test_a_charge_that_does_not_raise_soc_is_downgraded(self):
        slots, warnings = to_slots([
            iv(soc=15000, minute=0),
            iv(charge=900, imp=900, soc=14000, minute=15),
        ], CAPACITY)
        assert [s.action for s in slots] == ["hold"]
        assert "not above" in warnings[0]

    def test_the_guard_does_not_cross_a_plan_run_boundary(self):
        """THE BUG THIS EXISTS FOR, found on the real archive.

        After `newest_by_interval()`, adjacent intervals routinely come from different runs,
        and each run re-anchors to the battery's ACTUAL SoC at its own plan time. So the
        previous run's projection for this instant is a stale forecast, not a measurement --
        comparing across the seam compares two different trajectories.

        Every one of the six warnings this produced on real data fell exactly on a 3-hourly
        run boundary (e.g. 2026-08-13T21:00Z: outgoing run projected 5803 Wh, incoming
        re-anchored at 7021 Wh). No single-run synthetic fixture can reproduce it.
        """
        slots, warnings = to_slots([
            iv(soc=15000, minute=0, run="2026-08-01T09:00:00Z"),
            iv(discharge=900, exp=900, soc=16000, minute=15, run="2026-08-01T12:00:00Z"),
        ], CAPACITY)
        assert slots[1].action == "discharge"     # NOT downgraded
        assert warnings == []

    def test_the_first_interval_has_no_predecessor_to_check_against(self):
        slots, warnings = to_slots([
            iv(discharge=900, exp=900, soc=16000, minute=0),
            iv(soc=16000, minute=15),
        ], CAPACITY)
        assert slots[0].action == "discharge"
        assert warnings == []

    def test_power_rounding_to_zero_is_downgraded_loudly(self):
        slots, warnings = to_slots([
            iv(soc=14000, minute=0),
            iv(discharge=ENERGY_FLOOR_WH + 0.01, exp=900, soc=13000, minute=15),
        ], CAPACITY)
        assert slots[1].action in ("hold", "discharge")
        if slots[1].action == "hold":
            assert "rounds to" in warnings[-1]


class TestMerge:
    """Merging is deliberately narrower than `advise.py`'s. That collapses intervals into
    blocks because its reader is a human who does not want 104 rows; the dispatcher rewrites
    the register every 60 s regardless, so merging buys nothing operationally and costs the
    plan's per-interval power shaping."""

    def test_identical_adjacent_slots_merge(self):
        slots, _ = to_slots([iv(minute=m) for m in (0, 15, 30)], CAPACITY)
        assert len(slots) == 1
        assert slots[0].action == "hold"
        assert (slots[0].end - slots[0].start) == dt.timedelta(minutes=45)

    def test_a_differing_target_prevents_the_merge(self):
        """`target_soc` moves every interval while charging, which is exactly the intent."""
        slots, _ = to_slots([
            iv(charge=900, imp=900, soc=15000, minute=0),
            iv(charge=900, imp=900, soc=16000, minute=15),
        ], CAPACITY)
        assert len(slots) == 2

    def test_slots_stay_contiguous_and_half_open(self):
        slots, _ = to_slots([
            iv(charge=900, imp=900, soc=15000, minute=0),
            iv(discharge=900, exp=900, soc=14000, minute=15),
            iv(minute=30),
        ], CAPACITY)
        for a, b in zip(slots, slots[1:]):
            assert a.end == b.start


class TestSlotValidation:
    def test_a_command_slot_needs_power_and_target(self):
        t = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
        with pytest.raises(ValueError, match="requires power_w and target_soc"):
            Slot(t, t + dt.timedelta(minutes=15), "charge")

    def test_a_hold_slot_must_not_carry_them(self):
        t = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
        with pytest.raises(ValueError, match="must not carry"):
            Slot(t, t + dt.timedelta(minutes=15), "hold", 1000, 50.0)

    def test_unknown_action_rejected(self):
        t = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
        with pytest.raises(ValueError, match="unknown action"):
            Slot(t, t + dt.timedelta(minutes=15), "explode")

    def test_zero_length_slot_rejected(self):
        t = dt.datetime(2026, 8, 1, 12, tzinfo=UTC)
        with pytest.raises(ValueError, match="ends at or before"):
            Slot(t, t, "hold")


class TestBuildDocument:
    def _doc(self):
        return build_document(
            [iv(charge=900, imp=900, soc=15000, minute=0),
             iv(discharge=900, exp=900, soc=14000, minute=15)],
            CAPACITY, generated_at=dt.datetime(2026, 8, 1, 12, 5, tzinfo=UTC))

    def test_contract_keys_present(self):
        doc, _ = self._doc()
        assert set(doc) >= {"generated_at", "plan_run", "plan_runs", "horizon_end",
                            "interval_minutes", "capacity_wh", "slots"}

    def test_timestamps_are_utc_instants_not_local_strings(self):
        """Section 4.3: no local-clock string ever reaches the control path."""
        doc, _ = self._doc()
        assert doc["generated_at"].endswith("Z")
        assert all(s["start"].endswith("Z") and s["end"].endswith("Z") for s in doc["slots"])

    def test_horizon_end_is_the_last_slots_end(self):
        doc, _ = self._doc()
        assert doc["horizon_end"] == doc["slots"][-1]["end"]

    def test_generated_at_is_injected_not_read_from_the_clock(self):
        doc, _ = self._doc()
        assert doc["generated_at"] == "2026-08-01T12:05:00Z"

    def test_too_few_intervals_raises(self):
        with pytest.raises(PlanFormatError, match="at least two intervals"):
            build_document([iv()], CAPACITY, generated_at=dt.datetime.now(UTC))


class TestSyntheticSelfHeavyDay:
    """Real runs give 1-2 `self` intervals each -- enough to show the classifier fires, too
    thin to review as a pattern or catch a regression in."""

    @pytest.fixture
    def slots(self):
        ivs = from_table((FIXTURES / "synthetic_self_heavy.txt").read_text(), "synthetic")
        s, _ = to_slots(ivs, CAPACITY)
        return s

    def test_self_is_the_dominant_action(self, slots):
        covered = sum((s.end - s.start).total_seconds() for s in slots if s.action == "self")
        total = sum((s.end - s.start).total_seconds() for s in slots)
        assert covered / total > 0.6

    def test_both_flavours_of_self_appear(self, slots):
        """Cover-the-load and absorb-the-surplus reach `self` by different branches."""
        ivs = from_table((FIXTURES / "synthetic_self_heavy.txt").read_text(), "synthetic")
        kinds = {("discharge" if i.discharge_wh > ENERGY_FLOOR_WH else "charge")
                 for i in ivs if classify(i) == "self"}
        assert kinds == {"charge", "discharge"}

    def test_a_priced_export_still_becomes_a_real_command(self, slots):
        assert any(s.action == "discharge" and s.power_w for s in slots)

    def test_self_slots_carry_no_power_or_target(self, slots):
        for s in slots:
            if s.action == "self":
                assert s.power_w is None and s.target_soc is None


class TestSyntheticDstNight:
    def test_slots_stay_contiguous_across_the_repeated_hour(self):
        ivs = from_table((FIXTURES / "synthetic_dst_autumn.txt").read_text(), "synthetic")
        slots, _ = to_slots(ivs, CAPACITY)
        for a, b in zip(slots, slots[1:]):
            assert a.end == b.start, f"gap or overlap at {a.end.isoformat()}"

    def test_the_horizon_covers_every_utc_instant_once(self):
        ivs = from_table((FIXTURES / "synthetic_dst_autumn.txt").read_text(), "synthetic")
        slots, _ = to_slots(ivs, CAPACITY)
        assert slots[0].start == dt.datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
        assert slots[-1].end == dt.datetime(2026, 10, 25, 3, 0, tzinfo=UTC)


class TestSyntheticNegativePrices:
    def test_a_grid_charge_at_negative_prices_is_commanded(self):
        """The inversion: normally you would never import at midday."""
        ivs = from_table((FIXTURES / "synthetic_negative_prices.txt").read_text(), "synthetic")
        slots, warnings = to_slots(ivs, CAPACITY)
        charging = [s for s in slots if s.action == "charge"]
        assert charging
        assert all(s.power_w > 0 for s in charging)
        assert warnings == []


class TestHarvestAboveTheGauge:
    """`hold` -> `self` when the plan is at its own full and the house is exporting.

    THE MEASUREMENT THIS RESTS ON. Over 25 days of collector data (2026-07-22..2026-08-15),
    once `soc_percent` reads 100 the battery keeps absorbing a mean of 1,375 Wh/day (max
    2,240, on 16 of 25 days). It is real: 18,615 of the 22,005 Wh came back out before the
    gauge left 100 %, which is 85 % -- ordinary round-trip loss, not a phantom. And it is all
    solar; grid-sourced charge across those 1,075 minutes was 0 Wh.

    WHY A COMMAND CANNOT DO THIS. `target_soc_pct` is a percentage OF THE GAUGE, so at a
    saturated 100 % a Mode 2 command asking for 100 has nothing left to ask for. Only
    self-consumption keeps absorbing. `self` here is not a preference, it is the only
    mechanism that reaches the energy.
    """

    def test_at_planned_full_with_surplus_it_releases(self):
        assert classify(iv(soc=CAPACITY, exp=200), capacity_wh=CAPACITY) == "self"

    def test_at_planned_full_while_importing_it_still_holds(self):
        """The case that makes the rule safe. On 2026-08-05 the same `hold` run flips from
        exporting to importing 60 Wh at 18:00, three intervals before a 1,175 Wh/interval
        dump into the evening peak. `self` there would spend exactly the inventory the dump
        depends on, so the deficit half has to stay frozen."""
        assert classify(iv(soc=CAPACITY, imp=60), capacity_wh=CAPACITY) == "hold"

    def test_below_capacity_it_still_holds_even_with_surplus(self):
        """Not timidity -- jurisdiction. With room and surplus the LP had the option to
        charge and declined; that is a decision (a cheaper trough later, or the export being
        worth more). At capacity it is not deciding, it has run out of the variable, and that
        is the only place this rule is entitled to act."""
        assert classify(iv(soc=CAPACITY - 3000, exp=900), capacity_wh=CAPACITY) == "hold"

    def test_without_a_capacity_the_rule_is_off(self):
        """`classify` stays callable on an interval alone, and then behaves as it did before
        2026-08-15. Several callers and every older test rely on that."""
        assert classify(iv(soc=CAPACITY, exp=900)) == "hold"

    def test_surplus_at_the_energy_floor_still_harvests(self):
        """Regression on the threshold choice. `ENERGY_FLOOR_WH` asks "did the LP intend a
        dispatch"; this asks "is any solar going to the meter", and they are not the same
        question. Reusing 50 Wh dropped three of the four harvestable intervals on
        2026-08-05, whose exports sit at exactly 50 Wh."""
        assert classify(iv(soc=CAPACITY, exp=ENERGY_FLOOR_WH), capacity_wh=CAPACITY) == "self"

    @pytest.mark.parametrize("exp", [0.0, 5.0, SURPLUS_FLOOR_WH])
    def test_surplus_at_or_below_the_surplus_floor_is_noise(self, exp):
        assert classify(iv(soc=CAPACITY, exp=exp), capacity_wh=CAPACITY) == "hold"

    def test_the_full_test_has_a_tolerance(self):
        """`soc_wh` is an LP output in floating point; requiring exact equality with capacity
        would make the rule fire or not on the last bit."""
        assert classify(
            iv(soc=CAPACITY - FULL_TOLERANCE_WH + 1, exp=200), capacity_wh=CAPACITY) == "self"
        assert classify(
            iv(soc=CAPACITY - FULL_TOLERANCE_WH - 1, exp=200), capacity_wh=CAPACITY) == "hold"

    def test_commanded_intervals_are_untouched(self):
        """The rule reaches the standing-still branch only. A real charge or discharge at
        full is still a command."""
        assert classify(iv(soc=CAPACITY, discharge=1000, exp=900),
                        capacity_wh=CAPACITY) == "discharge"
        assert classify(iv(soc=CAPACITY, charge=1000, imp=900),
                        capacity_wh=CAPACITY) == "charge"

    def test_a_plan_balanced_interval_at_full_also_releases(self):
        """The 2026-08-06 17:00 case. The plan forecasts PV exactly meeting the house, so it
        has the battery neither charging nor discharging -- `hold` and `self` are identical
        in the plan's OWN model, and releasing costs nothing it was counting on.

        Worth releasing because the balance point is where the plan is least reliable:
        `export_wh` is a difference of two forecasts, so where they cancel its sign is set
        by the error. Measured, the PV forecast runs ~2.3x low at 17:00 local (194 Wh vs
        449 Wh actual, low on 88 % of intervals) while the load forecast is accurate.
        """
        assert classify(iv(soc=CAPACITY, exp=0, imp=0, pv=190),
                        capacity_wh=CAPACITY) == "self"

    def test_a_balanced_interval_with_no_sun_still_holds(self):
        """The guard that makes the balanced branch safe year-round rather than a summer
        rule. In a balanced interval `pv_forecast_wh` equals the household load by identity,
        so requiring sun means the rule only fires where the plan says solar covers the whole
        house -- it self-gates in winter and cannot fire at night."""
        assert classify(iv(soc=CAPACITY, exp=0, imp=0, pv=0), capacity_wh=CAPACITY) == "hold"

    def test_a_forecast_deficit_freezes_whatever_the_sun_is_doing(self):
        """Strong sun does not buy back the deficit branch. This is the 18:00 case on
        2026-08-05: releasing spends the inventory the 18:45 peak dump depends on, and the
        p10 outcome drains ~175 Wh per interval out of it."""
        assert classify(iv(soc=CAPACITY, imp=98, pv=400), capacity_wh=CAPACITY) == "hold"

    def test_a_downgraded_command_is_never_routed_to_the_harvest_release(self):
        """A downgrade is a FAULT path -- the plan asked for something the register cannot
        express -- and freezing is the conservative answer once the plan has stopped making
        sense. It is also unreachable for a charge by construction: `charge` requires
        `import_wh > ENERGY_FLOOR_WH`, which contradicts the near-zero-import test."""
        ivs = [
            iv(minute=0, soc=CAPACITY),
            PlanInterval(
                start=dt.datetime(2026, 8, 1, 12, 15, tzinfo=UTC), soc_wh=CAPACITY,
                charge_wh=1000.0, discharge_wh=0.0, import_wh=900.0, export_wh=200.0,
                plan_run="2026-08-01T09:00:00Z"),
        ]
        slots, warnings = to_slots(ivs, CAPACITY)
        assert any("downgraded to hold" in w for w in warnings), warnings
        assert slots[-1].action == "hold"

    def test_a_mixed_hold_run_splits_where_the_surplus_ends(self):
        """The 2026-08-05 shape, in miniature. The rule is decided per INTERVAL, before
        merging, so one flat `hold` block becomes `self` while exporting and `hold` once the
        house goes short -- which is the whole reason it is safe to apply at all."""
        ivs = [iv(minute=m, soc=CAPACITY, exp=200) for m in (0, 15)]
        ivs += [PlanInterval(start=dt.datetime(2026, 8, 1, 12, m, tzinfo=UTC),
                             soc_wh=CAPACITY, charge_wh=0.0, discharge_wh=0.0,
                             import_wh=60.0, export_wh=0.0, plan_run="2026-08-01T09:00:00Z")
                for m in (30, 45)]
        slots, _ = to_slots(ivs, CAPACITY)
        assert [s.action for s in slots] == ["self", "hold"]
        assert slots[0].end == slots[1].start == dt.datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
