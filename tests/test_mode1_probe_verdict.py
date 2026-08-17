"""The Mode 1 probe's two pure functions. `dispatch/test_mode1_negative.py`.

`plan_command()` and the verdict functions are total functions of their arguments -- no
Modbus, no clock -- which is the same property `registers.py` cites as the reason its
encodings get round-trip tests. They were previously "verified offline" against synthesised
samples, and that verification was captured nowhere: the version that shipped would have
printed HONOURED for a battery sitting flat at 0 W, and a three-line sample set says so
immediately.

The file under test is not collected by pytest (`testpaths = ["tests"]`), but `dispatch` is
on `pythonpath`, so it imports directly. It is named differently from the module it tests to
avoid a basename collision during collection.

Assertions are on the verdict TOKEN, never the prose, so the operator-facing text stays
editable without touching tests.
"""
from __future__ import annotations

import pytest

import test_mode1_negative as probe

# A baseline the pre-flight would actually accept: 1200 W into the battery, which
# `plan_command` turns into a 600 W command with 600 W of separation and a 200 W tolerance.
# Bands: flat 0-200, honoured 400-800, ignored 1000-1400.
BASE_CHARGE = 1200
WANT = 600
COMMANDED = -WANT           # register convention: negative charges
SOLAR = 3000


def sample(charge, export, *, commanded_w=COMMANDED, start=1, mode=1, solar=SOLAR):
    """One inverter sample, written in the two quantities the probe reasons about.

    `charge` is positive into the battery and `export` positive out of the house; both
    registers store the opposite sign, and getting that backwards is exactly the class of
    error these tests exist to catch.
    """
    return {
        "time": "12:00:00", "soc_pct": 50.0,
        "battery_w": -charge, "grid_w": -export, "solar_w": solar,
        "d_start": start, "d_mode": mode, "d_power_w": commanded_w,
        "d_soc_pct": 60.0, "d_time_s": 240,
    }


def window(charge, export, n=6, **kw):
    return [sample(charge, export, **kw) for _ in range(n)]


def under(baseline, during, commanded_w=COMMANDED, aborted=False):
    return probe.verdict(baseline, during, commanded_w, aborted, undercommand=True)


def token(lines):
    return probe.verdict_token(lines)


class TestSampleHelper:
    """If the helper's signs are wrong, every assertion below is decoration."""

    def test_surplus_is_charge_plus_export(self):
        assert probe.surplus_of(sample(1200, 800)) == 2000

    def test_house_load_is_what_is_left_of_solar(self):
        assert probe.house_load(sample(1200, 800)) == SOLAR - 2000


class TestUndercommandVerdict:
    def test_honoured(self):
        """Charge lands on the setpoint and the power it stopped taking exports."""
        lines = under(window(BASE_CHARGE, 0), window(620, 580))
        assert token(lines) == "HONOURED"

    def test_ignored(self):
        """Battery carries on taking the whole baseline charge; export does not move."""
        lines = under(window(BASE_CHARGE, 0), window(BASE_CHARGE, 0))
        assert token(lines) == "IGNORED"

    def test_a_frozen_battery_is_flat_not_honoured(self):
        """The bug this file was written for.

        Charge 0 W was BELOW the midpoint of (commanded, baseline), and with the battery
        taking nothing the whole surplus exports -- so the original one-sided test saw both
        channels "agree" and printed `HONOURED. The battery took roughly what it was told to
        take` for a battery that took nothing at all. FLAT is checked first precisely because
        the export channel would otherwise rescue it.
        """
        lines = under(window(BASE_CHARGE, 0), window(0, BASE_CHARGE))
        assert token(lines) == "FLAT"

    def test_honoured_survives_a_non_zero_baseline_export(self):
        """1200 W into the battery with 1000 W already exporting, so surplus is 2200 W.

        Honoured means charge 620 W and export 1580 W -- a 580 W change. Judged against the
        SURPLUS the threshold would be (2200-600)/2 = 800 W and this reads INCONCLUSIVE;
        judged against the baseline CHARGE it is (1200-600)/2 = 300 W and it reads correctly.
        Only power the battery was already taking can be displaced.
        """
        lines = under(window(BASE_CHARGE, 1000), window(620, 1580))
        assert token(lines) == "HONOURED"

    def test_a_charge_between_both_predictions_is_inconclusive(self):
        lines = under(window(BASE_CHARGE, 0), window(900, 300))
        assert token(lines) == "INCONCLUSIVE"

    def test_an_aborted_window_is_inconclusive_not_ignored(self):
        """The app asserting `mode=2 dpwr=-5000` mid-window, as it did on 2026-08-15.

        Two samples import heavily, `watch` cuts the window, and the charge is nowhere near
        either prediction while the grid swings to import. Without the `aborted` flag that
        reads as IGNORED -- a load-bearing architectural conclusion drawn at full confidence
        from somebody else's command.
        """
        during = window(5000, -3800, n=2)
        assert token(under(window(BASE_CHARGE, 0), during, aborted=True)) == "INCONCLUSIVE"

    def test_too_few_samples_is_inconclusive(self):
        during = window(620, 580, n=probe.MIN_DURING_SAMPLES - 1)
        assert token(under(window(BASE_CHARGE, 0), during)) == "INCONCLUSIVE"

    def test_the_load_drift_warning_still_fires(self):
        lines = under(window(BASE_CHARGE, 0), window(620, 580, solar=SOLAR + 2 * probe.LOAD_DRIFT))
        assert any("house load moved" in ln for ln in lines)


class TestTheHonouredBandHasRoomOnBothSides:
    """Sweep the observed charge across the whole range and read the boundaries off.

    A previous version put the FLAT cut at `want/2` while banding the other two at +/-tol.
    With the command at a third of baseline that cut landed INSIDE the honoured band, so
    HONOURED ran from 0.51x to 1.68x the setpoint and butted straight against FLAT -- a
    battery clamping the magnitude while honouring the sign printed "took roughly what it was
    told to take". The claim being certified is "Mode 1 is a real setpoint", and the design
    commands specific powers on that basis, so the low side needs a gap as much as the high
    side does.
    """

    def verdicts(self):
        base = window(BASE_CHARGE, 0)
        out = {}
        for charge in range(0, BASE_CHARGE + 401):
            during = window(charge, BASE_CHARGE - charge)
            out[charge] = token(under(base, during))
        return out

    def test_flat_and_honoured_are_separated_by_inconclusive(self):
        by_charge = self.verdicts()
        flat = [c for c, t in by_charge.items() if t == "FLAT"]
        honoured = [c for c, t in by_charge.items() if t == "HONOURED"]
        assert max(flat) < min(honoured) - 1, "FLAT and HONOURED still abut"
        assert all(by_charge[c] == "INCONCLUSIVE" for c in range(max(flat) + 1, min(honoured)))

    def test_the_honoured_band_is_centred_on_the_setpoint(self):
        honoured = [c for c, t in self.verdicts().items() if t == "HONOURED"]
        low, high = min(honoured) / WANT, max(honoured) / WANT
        assert 0.6 < low < 0.7, low
        assert 1.3 < high < 1.4, high

    def test_a_battery_clamping_at_half_the_setpoint_is_not_honoured(self):
        """The scenario that motivated this: sign honoured, magnitude clamped.

        0.5x is the ratio the old geometry called HONOURED (its low edge sat at 0.51x). It is
        now squarely in the dead zone between FLAT and HONOURED, which is the honest reading.
        """
        during = window(WANT // 2, BASE_CHARGE - WANT // 2)
        assert token(under(window(BASE_CHARGE, 0), during)) == "INCONCLUSIVE"


class TestTheRecordedLiveRun:
    """2026-08-16. The run whose HONOURED verdict `DESIGN-dispatch.md` §9 rests on.

    It was produced by the one-sided logic, which never checked that the charge was NEAR the
    setpoint -- so the conclusion needs re-deriving, not assuming. Reconstructed from the
    recorded medians (baseline grid -1 W, surplus ~1220 W, commanded 402 W, charge 331-361 W,
    export up ~1014 W); the raw trace is in the run's CSV, not here.
    """

    LIVE = -402

    def test_it_still_reads_honoured(self):
        baseline = window(1219, 1, commanded_w=self.LIVE)
        during = window(346, 1015, commanded_w=self.LIVE)
        lines = probe.verdict(baseline, during, self.LIVE, False, undercommand=True)
        assert token(lines) == "HONOURED"

    def test_a_re_run_today_would_command_more_than_that_run_did(self):
        """UNDER_FRACTION moved from a third to a half, so the same 1219 W baseline now asks
        for ~609 W rather than 402 W. The recorded run is still readable -- its charge sits
        well inside the honoured band for its own 402 W command -- but a re-run is not a
        repeat, and the doc should not be read as if it were."""
        assert probe.plan_command(1219, True)[0] == -609


class TestSamplesThatDidNotCarryTheCommand:
    """`start=1` at pre-flight is a hard stop, but nothing stopped the app starting after."""

    def test_hijacked_samples_are_dropped_and_flagged(self):
        good = window(620, 580, n=4)
        hijacked = window(5000, -3800, n=4, mode=2)
        lines = probe.verdict(window(BASE_CHARGE, 0), good + hijacked, COMMANDED, False,
                              undercommand=True)
        assert any("did not carry the command" in ln for ln in lines)
        # Unfiltered, the median charge over those eight samples is 5000 W, not 620 W.
        assert token(lines) == "HONOURED"

    def test_a_different_power_in_the_block_also_counts_as_hijacked(self):
        during = window(620, 580, n=6, commanded_w=-5000)
        assert probe.command_carrying(during, COMMANDED) == []

    def test_a_wholly_hijacked_window_is_inconclusive(self):
        during = window(5000, -3800, n=6, start=0)
        lines = probe.verdict(window(BASE_CHARGE, 0), during, COMMANDED, False,
                              undercommand=True)
        assert token(lines) == "INCONCLUSIVE"

    def test_a_dry_run_keeps_every_sample(self):
        """Nothing was written, so no sample can carry the command. Filtering would eat them
        all and make every dry run report INCONCLUSIVE."""
        during = window(620, 580, start=0, commanded_w=0)
        lines = probe.verdict(window(BASE_CHARGE, 0), during, COMMANDED, False,
                              undercommand=True, dry_run=True)
        assert token(lines) == "HONOURED"
        assert not any("did not carry the command" in ln for ln in lines)


class TestOvercommandVerdictStillWorks:
    """The extraction must not have moved the probe that has already answered its question."""

    OVER = -4790                              # 2790 W surplus + the 2000 W margin

    def over(self, during, aborted=False):
        baseline = window(2790, 0, commanded_w=self.OVER)
        return probe.verdict(baseline, during, self.OVER, aborted)

    def test_cap(self):
        during = window(2790, 0, commanded_w=self.OVER)
        assert token(self.over(during)) == "CAP"

    def test_demand(self):
        during = window(5000, -2210, commanded_w=self.OVER)   # importing 2210 W to charge 5000
        assert token(self.over(during)) == "DEMAND"

    def test_flat(self):
        during = window(0, 2790, commanded_w=self.OVER)
        assert token(self.over(during)) == "FLAT"

    def test_no_samples_at_all(self):
        assert token(self.over([])) == "INCONCLUSIVE"


class TestPlanCommand:
    def test_the_undercommand_setpoint_is_a_fraction_of_the_baseline_charge(self):
        watts, _ = probe.plan_command(BASE_CHARGE, True)
        assert watts == -int(BASE_CHARGE * probe.UNDER_FRACTION)

    def test_separation_too_small_refuses_to_run(self):
        watts, why = probe.plan_command(probe.MIN_CHARGE_UNDER - 300, True)
        assert watts is None
        assert "Re-run in brighter light" in why

    def test_the_gate_and_the_separation_rule_agree(self):
        """MIN_CHARGE_UNDER exists so the pre-flight never admits a run that `plan_command`
        then refuses -- which would waste a scarce clear-sky window on an abort. Every
        baseline the gate lets through must produce a command."""
        for available in range(probe.MIN_CHARGE_UNDER, 8000, 25):
            assert probe.plan_command(available, True)[0] is not None, available

    def test_the_min_command_floor_never_binds_on_a_runnable_probe(self):
        """Documenting a real consequence of the constants: any baseline charge large enough
        to clear MIN_SEPARATION is also large enough that a third of it exceeds MIN_COMMAND.
        The floor is belt-and-braces, not live logic."""
        for available in range(200, 8000, 25):
            watts, _ = probe.plan_command(available, True)
            if watts is not None:
                assert abs(watts) == int(available * probe.UNDER_FRACTION)

    def test_all_three_prediction_bands_stay_separated_by_a_real_gap(self):
        """The geometry the whole verdict rests on.

        Three predictions -- 0, want, base_charge -- each with a band one tolerance wide. If
        any two bands touch, a single charge reading satisfies two hypotheses at once and the
        verdict stops meaning anything. The FLAT/HONOURED pair is the one that has been wrong
        twice, so it is asserted at the same strength as the other.
        """
        for available in range(probe.MIN_CHARGE_UNDER, 8000, 25):
            watts, _ = probe.plan_command(available, True)
            assert watts is not None, available
            want = abs(watts)
            tol = max(probe.CHARGE_TOL_FLOOR, want / 3)
            assert tol < want - tol, f"flat and honoured touch at {available}"
            assert want + tol < available - tol, f"honoured and ignored touch at {available}"

    def test_max_power_clamps_the_undercommand_setpoint(self):
        watts, _ = probe.plan_command(20000, True)
        assert watts == -probe.MAX_POWER

    def test_overcommand_exceeds_surplus_by_the_margin(self):
        watts, why = probe.plan_command(2790, False)
        assert watts == -(2790 + probe.MARGIN)
        assert "exceeds" in why

    def test_overcommand_refuses_when_the_ceiling_is_too_close(self):
        watts, why = probe.plan_command(4000, False)
        assert watts is None
        assert "ceiling" in why


class TestVerdictToken:
    def test_prose_without_a_verdict_has_no_token(self):
        assert probe.verdict_token(["!! surplus was marginal"]) is None

    @pytest.mark.parametrize("expected", ["HONOURED", "IGNORED", "FLAT", "INCONCLUSIVE"])
    def test_every_undercommand_outcome_is_reachable(self, expected):
        cases = {
            "HONOURED": (window(BASE_CHARGE, 0), window(620, 580), False),
            "IGNORED": (window(BASE_CHARGE, 0), window(BASE_CHARGE, 0), False),
            "FLAT": (window(BASE_CHARGE, 0), window(0, BASE_CHARGE), False),
            "INCONCLUSIVE": (window(BASE_CHARGE, 0), window(900, 300), False),
        }
        baseline, during, aborted = cases[expected]
        assert token(under(baseline, during, aborted=aborted)) == expected
