"""The dry-run review's gap classifier, and the constants it shares with the other scripts.

Loaded by path because `scripts/review-dry-run.py` is hyphenated and `scripts/` is not on
`pythonpath` -- it is an operator tool run from the Mac, not a module the container imports,
and putting it on the path would let production code import it by accident.

WHY THIS FILE EXISTS. The page's gap fault used to assert that a silence was "the loop not
running", which it has no way of knowing. On 2026-08-17/18 every one of six gaps was a home
network stall that took out the collector at the same time, and the page called each of them a
dispatch fault -- on the most alarming item it displays, in the one section whose whole job is
to gate going live. The classifier below is the fix, and these are the cases that decide it.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


review = _load("review_dry_run", "review-dry-run.py")
deciding = _load("is_it_deciding", "is-it-deciding.py")

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


def classify(tick_rows, battery):
    """(faults, stalls) for a set of ticks against a collector series."""
    runs = review.decision_runs(tick_rows)
    faults, _previews, stalls = review.findings(
        tick_rows, runs, battery, samples(), review.DEFAULT_SOC_FLOOR)
    return faults, stalls


class TestLongestHole:
    def test_an_empty_series_is_one_hole_the_width_of_the_window(self):
        assert review.longest_hole([], at(0), at(5)) == 5 * 60

    def test_the_window_edges_bound_the_hole(self):
        # The only sample sits in the middle, so neither the run up to it nor the run after
        # it may be reported as zero -- a collector that stops before the window and resumes
        # after it is exactly the shape that matters.
        assert review.longest_hole([(at(2), 1.0)], at(0), at(5)) == 3 * 60

    def test_it_reports_the_longest_hole_not_the_last(self):
        series = [(at(1), 1.0), (at(6), 1.0), (at(7), 1.0)]
        assert review.longest_hole(series, at(0), at(8)) == 5 * 60


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

    Both say so in their own comments; nothing enforced it. The two disagreeing would be worse
    than either being wrong, because the quick check would clear a dispatcher the day-long
    review is about to fail, and there would be no reason to look further.
    """

    @pytest.mark.parametrize("name", ["TICK_S", "GAP_S", "STALE_PLAN_S"])
    def test_the_two_scripts_use_the_same_thresholds(self, name):
        assert getattr(review, name) == getattr(deciding, name)

    def test_the_match_pad_is_wide_enough_to_be_worth_having(self):
        # Narrower than one collector sample interval and the padding could not fix any
        # misalignment; wider than the gap threshold and it starts reaching into whole
        # unrelated gaps.
        assert review.COLLECTOR_GAP_S <= review.MATCH_PAD_S * 2
        assert review.MATCH_PAD_S < review.GAP_S * 2
