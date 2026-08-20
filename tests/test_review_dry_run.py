"""The gap classifier, and the constants the operator scripts share with it.

The judgement lives in `dispatch/reliability.py` and is imported normally. The two scripts
are loaded by path because they are hyphenated and `scripts/` is not on `pythonpath` -- they
are operator tools run from the Mac, not modules the container imports, and putting them on
the path would let production code import them by accident.

The classifier is exercised THROUGH the renderer rather than beside it, because the pair is
what a reader sees: an analysis that finds a stall and a renderer that calls it a fault
would pass two tests and still print the wrong thing.

WHY THIS FILE EXISTS. The page's gap fault used to assert that a silence was "the loop not
running", which it has no way of knowing. On 2026-08-17/18 every one of six gaps was a home
network stall that took out the collector at the same time, and the page called each of them a
dispatch fault -- on the most alarming item it displays, in the one section whose whole job is
to gate going live. The classifier below is the fix, and these are the cases that decide it.
"""
from __future__ import annotations

import datetime as dt

import pytest

import reliability
from conftest import REPO, load_script

review = load_script("review_dry_run", "review-dry-run.py")
deciding = load_script("is_it_deciding", "is-it-deciding.py")

BASE = dt.datetime(2026, 8, 18, 0, 0, tzinfo=dt.UTC)


def at(minutes: float) -> dt.datetime:
    return BASE + dt.timedelta(minutes=minutes)


def ticks(*, gap_at: float, gap_minutes: float, total: float = 40) -> list[dict]:
    """One tick a minute, with a single hole of `gap_minutes` starting at `gap_at`."""
    out = []
    m = 0.0
    while m < total:
        if not gap_at <= m < gap_at + gap_minutes:
            out.append({"_time": at(m), "slot_action": "hold", "action": "no dispatch"})
        m += 1
    return out


def samples(*, hole_from: float | None = None, hole_to: float | None = None,
            total: float = 40) -> list[tuple[dt.datetime, float]]:
    """The collector's one-minute series, optionally missing [hole_from, hole_to)."""
    out = []
    m = 0.0
    while m < total:
        if hole_from is None or not hole_from <= m < hole_to:
            out.append((at(m), -500.0))
        m += 1
    return out


def slot_rows(action: str | None = "hold", *, minutes: int = 5, **extra) -> list[dict]:
    """`minutes` consecutive ticks all deciding the same slot action.

    `action=None` writes NO `slot_action` field, which is the real shape of a tick outside
    the plan's horizon -- `decision_runs` reads that as `self`, and that is the run the
    divergence check skips.
    """
    out = []
    for m in range(minutes):
        row = {"_time": at(m), "action": "no dispatch", **extra}
        if action is not None:
            row["slot_action"] = action
        out.append(row)
    return out


def flat(w: float, *, total: float = 40) -> list[tuple[dt.datetime, float]]:
    """The collector's series holding a constant power.

    THE SIGN IS THE COLLECTOR'S, not the dashboard's: `battery_power_w` is positive while
    the battery DISCHARGES. `analyse` flips it once so both sides of the comparison are
    charging-positive, and the tests below straddle that flip on purpose.
    """
    return [(at(m), w) for m in range(int(total))]


def pcts(pct: float, *, total: float = 40) -> list[tuple[dt.datetime, float]]:
    """A flat SoC series, in PERCENT -- `rendered`'s default, and its own unit.

    It used to default to `samples()`, a power series in watts. That was invisible while
    every run was a hold, because the floor check reads SoC only under a discharge; the
    first discharge test would have read -500 "percent", fired `discharge_below_floor`, and
    blamed the code. A fixture whose units are wrong only under the case you have not
    written yet is worse than no fixture.
    """
    return [(at(m), pct) for m in range(int(total))]


def classify(tick_rows, battery):
    """(faults, stalls) for a set of ticks against a collector series, as rendered."""
    found = rendered(tick_rows, battery)
    return found["fault"], found["stall"]


def rendered(tick_rows, battery, soc=None):
    """{severity: [sentence]} -- the analysis run through the page's own renderer."""
    runs = reliability.decision_runs(tick_rows)
    found = reliability.by_severity(reliability.analyse(
        tick_rows, runs, battery, pcts(50.0) if soc is None else soc,
        reliability.DEFAULT_SOC_FLOOR))
    return {sev: [review.render(f) for f in fs] for sev, fs in found.items()}


class TestLongestHole:
    def test_an_empty_series_is_one_hole_the_width_of_the_window(self):
        assert reliability.longest_hole([], at(0), at(5)) == 5 * 60

    def test_the_window_edges_bound_the_hole(self):
        # The only sample sits in the middle, so neither the run up to it nor the run after
        # it may be reported as zero -- a collector that stops before the window and resumes
        # after it is exactly the shape that matters.
        assert reliability.longest_hole([(at(2), 1.0)], at(0), at(5)) == 3 * 60

    def test_it_reports_the_longest_hole_not_the_last(self):
        series = [(at(1), 1.0), (at(6), 1.0), (at(7), 1.0)]
        assert reliability.longest_hole(series, at(0), at(8)) == 5 * 60


class TestGapClassification:
    def test_a_gap_the_collector_shared_is_a_stall_not_a_fault(self):
        faults, stalls = classify(ticks(gap_at=10, gap_minutes=5),
                                  samples(hole_from=10, hole_to=15))
        assert faults == []
        assert len(stalls) == 1
        assert "upstream of both" in stalls[0]

    def test_a_gap_the_collector_survived_is_a_dispatch_fault(self):
        faults, stalls = classify(ticks(gap_at=10, gap_minutes=5), samples())
        assert stalls == []
        assert len(faults) == 1
        assert "this one is the dispatch loop" in faults[0]

    def test_the_two_stalls_need_not_line_up(self):
        """The regression that motivated `MATCH_PAD_S`.

        Dispatch stops first and recovers first; the collector holds on until near the end of
        the dispatch gap and comes back after it. Measured strictly inside the gap the shared
        hole is 120 s -- under `COLLECTOR_GAP_S` -- and the real 02:14 stall of 2026-08-18 was
        called a dispatch fault on exactly that arithmetic. Padded, it is 240 s.

        The numbers are chosen to straddle the threshold: drop `MATCH_PAD_S` to 0 and this
        test must fail, or it is not testing the padding at all.
        """
        faults, stalls = classify(ticks(gap_at=10, gap_minutes=5),
                                  samples(hole_from=14, hole_to=17))
        assert faults == []
        assert len(stalls) == 1

    def test_a_collector_hole_too_far_away_does_not_excuse_the_gap(self):
        # Half an hour later is a different event. If any hole anywhere counted, a single bad
        # afternoon would excuse every dispatch fault of the day.
        faults, stalls = classify(ticks(gap_at=10, gap_minutes=5, total=60),
                                  samples(hole_from=40, hole_to=45, total=60))
        assert stalls == []
        assert len(faults) == 1

    def test_with_no_collector_data_it_says_it_cannot_tell(self):
        faults, stalls = classify(ticks(gap_at=10, gap_minutes=5), [])
        assert stalls == []
        assert len(faults) == 1
        # The honest answer, and specifically NOT a claim about the loop.
        assert "no <code>power_readings</code> data" in faults[0]
        assert "this one is the dispatch loop" not in faults[0]

    def test_a_clean_run_produces_neither(self):
        faults, stalls = classify(ticks(gap_at=99, gap_minutes=0), samples())
        assert (faults, stalls) == ([], [])


class TestSharedConstants:
    """`is-it-deciding.py` and `review-dry-run.py` must agree on what a stalled loop is.

    Both used to say so in their own comments, over their own copies, and nothing enforced
    it. The two disagreeing would be worse than either being wrong, because the quick check
    would clear a dispatcher the day-long review is about to fail, and there would be no
    reason to look further.

    Now they import the same objects, so `is` rather than `==`: equal values would also be
    satisfied by two copies that happen to match today, which is the arrangement this
    replaced.
    """

    THRESHOLDS = ("TICK_S", "GAP_S", "STALE_PLAN_S", "COLLECTOR_GAP_S", "MATCH_PAD_S",
                  "IDLE_W")

    @pytest.mark.parametrize("script", [review, deciding],
                             ids=["review-dry-run", "is-it-deciding"])
    def test_every_threshold_a_script_names_is_the_shared_one(self, script):
        """Each script imports the subset it uses -- `review-dry-run.py` no longer names
        GAP_S at all, because the gap logic moved with it. What is asserted is that anything
        it DOES name is the same object, not a local copy that agrees today.
        """
        seen = [n for n in self.THRESHOLDS if hasattr(script, n)]
        assert seen, f"{script.__name__} names no shared threshold -- guard is vacuous"
        for name in seen:
            assert getattr(script, name) is getattr(reliability, name), name

    def test_both_scripts_query_the_same_fields(self):
        """The drift that actually happened. `read_error` belonged in both lists and was in
        neither, so a degraded tick was invisible to the page AND to the quick check."""
        assert deciding.FIELDS is reliability.FIELDS
        assert "read_error" in reliability.FIELDS

    def test_the_match_pad_is_wide_enough_to_be_worth_having(self):
        # Narrower than one collector sample interval and the padding could not fix any
        # misalignment; wider than the gap threshold and it starts reaching into whole
        # unrelated gaps.
        assert reliability.COLLECTOR_GAP_S <= reliability.MATCH_PAD_S * 2
        assert reliability.MATCH_PAD_S < reliability.GAP_S * 2

    def test_neither_script_redeclares_a_threshold(self):
        """A re-added `GAP_S = 180` at module scope would shadow the import and pass every
        test above, because 180 is still 180. It is the next edit that diverges."""
        for name in ("review-dry-run.py", "is-it-deciding.py"):
            src = (REPO / "scripts" / name).read_text()
            for const in ("TICK_S", "GAP_S", "STALE_PLAN_S", "COLLECTOR_GAP_S",
                          "MATCH_PAD_S", "IDLE_W", "FIELDS"):
                assert f"\n{const} = " not in src, f"{name} redeclares {const}"


class TestTheArmedPredicate:
    """A degraded tick publishes NO `action` -- `state.py` is explicit that the honest report
    of an unreadable inverter is a missing field, not a stale one.

    Read as `(t.get("action") or "") != "no dispatch"`, that missing field became `""`, `""`
    is not `"no dispatch"`, and an ordinary Modbus timeout was reported as the most alarming
    finding the page has: the dispatch block ARMED by a foreign controller, with the reason
    rendering the literal string `"None"`.
    """

    def test_a_degraded_tick_is_not_armed(self):
        rows = [{"_time": at(m), "slot_action": "hold", "read_error": "timed out"}
                for m in range(5)]
        assert reliability.armed_ticks(rows) == []

    def test_a_genuinely_armed_tick_still_is(self):
        rows = [{"_time": at(0), "action": "charge at 3000 W"}]
        assert reliability.armed_ticks(rows) == rows

    def test_the_ordinary_case_is_not_armed(self):
        rows = [{"_time": at(0), "action": "no dispatch"}]
        assert reliability.armed_ticks(rows) == []

    def test_the_page_does_not_accuse_a_timed_out_inverter(self):
        rows = [{"_time": at(m), "slot_action": "hold", "read_error": "timed out"}
                for m in range(5)]
        found = rendered(rows, samples())
        assert not any("ARMED" in f for f in found["fault"]), found["fault"]


class TestDegradedTicks:
    """Before `read_error` was queried these ticks were not rows at all.

    They contributed nothing to the pivot, so a run of them read as a GAP -- the page
    reported an unreachable inverter as a dead loop, which is the opposite diagnosis.
    """

    def test_they_are_reported_and_are_not_a_fault(self):
        rows = [{"_time": at(m), "slot_action": "hold", "read_error": "timed out"}
                for m in range(5)]
        found = rendered(rows, samples())
        assert found["fault"] == []
        assert len(found["degraded"]) == 1
        assert "could not read the inverter" in found["degraded"][0]

    def test_a_run_of_them_is_not_a_gap(self):
        """The whole point of adding the field: these minutes are accounted for."""
        rows = [{"_time": at(m), "slot_action": "hold", "action": "no dispatch"}
                for m in range(5)]
        rows += [{"_time": at(m), "slot_action": "hold", "read_error": "timed out"}
                 for m in range(5, 20)]
        rows += [{"_time": at(m), "slot_action": "hold", "action": "no dispatch"}
                 for m in range(20, 25)]
        found = rendered(rows, samples())
        assert not any("no decision" in f for f in found["fault"]), found["fault"]

    def test_dropping_them_is_what_produced_the_phantom_gap(self):
        """The counterfactual, so the test above is shown to be testing something."""
        rows = [{"_time": at(m), "slot_action": "hold", "action": "no dispatch"}
                for m in list(range(5)) + list(range(20, 25))]
        found = rendered(rows, samples())
        assert any("no decision" in f for f in found["fault"]), found["fault"]

    def test_a_clean_day_reports_nothing_degraded(self):
        found = rendered(ticks(gap_at=99, gap_minutes=0), samples())
        assert found["degraded"] == []


class TestPlanAge:
    """`plan_age_s`, and the stale-plan fault built on it.

    The tags are not comparable as strings -- runs written before 2026-07-30 carry `+02:00`
    where later ones carry `Z` -- which is why this goes through `plan.run_time` rather than
    subtracting text. `TODO.md` item 4 is the same trap, still live in
    `test_dispatch_goldens.py`.
    """

    def test_a_tick_that_named_no_plan_has_no_age(self):
        # None rather than 0, and the distinction is load-bearing: `analyse` filters these
        # out before the comparison, and `None > STALE_PLAN_S` is a TypeError, not a False.
        assert reliability.plan_age_s({"_time": at(0)}) is None
        assert reliability.plan_age_s({"_time": at(0), "plan_run": ""}) is None

    def test_a_fresh_plan_is_not_stale(self):
        found = rendered(slot_rows(plan_run="2026-08-17T23:30:00Z"), samples())
        assert found["fault"] == []

    def test_an_old_plan_is_a_fault_reporting_the_worst_age(self):
        found = rendered(slot_rows(plan_run="2026-08-17T20:00:00Z"), samples())
        assert len(found["fault"]) == 1
        assert "5 tick(s) acted on a plan over 2 h old" in found["fault"][0]
        # Four minutes past four hours, on the last of the five ticks.
        assert "worst 4.1 h" in found["fault"][0]

    def test_the_offset_spelling_is_read_as_an_instant_not_as_text(self):
        """The archive's other spelling, chosen to straddle the threshold.

        `23:30+02:00` is `21:30Z`, so these ticks acted on a plan 2.5 h old and are stale.
        Read the tag as if the offset were not there and it is 30 minutes old and fine --
        so this test flips outright if `run_time` is ever replaced by naive parsing.
        """
        found = rendered(slot_rows(plan_run="2026-08-17T23:30:00+02:00"), samples())
        assert len(found["fault"]) == 1
        assert "worst 2.6 h" in found["fault"][0]


class TestDischargeBelowFloor:
    """`slots.decide()` refuses a discharge below the floor, so one decided here is a
    translator bug -- the only decision on the page that is a fault rather than a preview.
    """

    def test_a_discharge_under_the_floor_is_a_fault(self):
        found = rendered(slot_rows("discharge"), flat(500.0), soc=pcts(8.0))
        assert len(found["fault"]) == 1
        assert "discharge decided at 8.0% SoC" in found["fault"][0]
        assert "below the 10% floor" in found["fault"][0]

    def test_a_discharge_above_the_floor_is_not(self):
        found = rendered(slot_rows("discharge"), flat(500.0), soc=pcts(50.0))
        assert found["fault"] == []

    def test_only_a_discharge_is_measured_against_the_floor(self):
        # Charging at 8% is not a fault, it is the whole point. A check that fired on any
        # low-SoC run would flag every winter morning.
        found = rendered(slot_rows("charge"), flat(-500.0), soc=pcts(8.0))
        assert found["fault"] == []

    def test_the_floor_is_the_callers_and_not_a_constant(self):
        # `--soc-floor` exists because the inverter's own floor is configurable. Asserted
        # against `analyse` directly: the sentence renders `DEFAULT_SOC_FLOOR` either way.
        rows = slot_rows("discharge")
        runs = reliability.decision_runs(rows)

        def kinds(floor):
            return [f.kind for f in reliability.analyse(
                rows, runs, flat(500.0), pcts(8.0), floor)]

        assert "discharge_below_floor" not in kinds(5.0)
        assert "discharge_below_floor" in kinds(20.0)


class TestDivergence:
    """What going live would have CHANGED -- a preview, not a fault.

    Every case here turns on the sign flip in `analyse`: the collector reports discharge as
    positive and `setpoint_w` reports charge as positive, so the comparison is wrong by a
    whole direction if that flip is dropped.
    """

    def test_a_hold_against_a_moving_battery_is_a_preview(self):
        # The largest behaviour change the page reports, and the easiest to overlook,
        # because "hold" sounds like "do nothing".
        found = rendered(slot_rows("hold"), flat(-500.0), soc=pcts(50.0))
        assert found["fault"] == []
        assert len(found["preview"]) == 1
        assert "frozen at 0 W" in found["preview"][0]

    def test_a_hold_against_an_idle_battery_is_not(self):
        found = rendered(slot_rows("hold"), flat(-10.0), soc=pcts(50.0))
        assert found["preview"] == []

    def test_a_charge_the_battery_was_already_doing_is_not_a_preview(self):
        found = rendered(slot_rows("charge"), flat(-500.0), soc=pcts(50.0))
        assert found["preview"] == []

    def test_a_charge_against_a_discharging_battery_is(self):
        """The sign convention, pinned end to end.

        The collector says +500 (discharging); the page must say -500 W, having flipped it
        into the charging-positive frame every other number on the dashboard uses. Drop the
        flip and this reads `+500 W` against a decision to charge -- agreement, reported.
        """
        found = rendered(slot_rows("charge"), flat(500.0), soc=pcts(50.0))
        assert len(found["preview"]) == 1
        assert "-500 W" in found["preview"][0]

    def test_a_discharge_the_battery_was_already_doing_is_not_a_preview(self):
        found = rendered(slot_rows("discharge"), flat(500.0), soc=pcts(50.0))
        assert found["preview"] == []

    def test_self_is_skipped_because_it_is_a_deliberate_release(self):
        # No `slot_action` at all, which is what a tick outside the horizon looks like. The
        # battery running self-consumption under it is the decision being HONOURED, so there
        # is nothing going live would change.
        found = rendered(slot_rows(None), flat(-500.0), soc=pcts(50.0))
        assert found["preview"] == []


def test_by_severity_has_every_severity_even_with_nothing_to_report():
    """The good day, which is the one nobody exercises.

    A renderer picks its "nothing to report" branch on an empty list; a missing key would
    make the clean run the only run that raises.
    """
    assert reliability.by_severity([]) == {s: [] for s in reliability.SEVERITIES}
    assert set(reliability.SEVERITIES) == {"fault", "preview", "stall", "degraded"}
