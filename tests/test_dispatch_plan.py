"""Reading the planner's output. `dispatch/plan.py`.

The synthetic fixtures under `tests/fixtures/plans/` cover the three shapes the real August
archive physically cannot contain: a DST transition, negative prices, and a `self`-heavy day.
Everything else is tested against the real corpus (see test_dispatch_invariants.py), because
inventing plan data teaches the tests only what the author already believed.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from plan import (
    PLANNER_TZ,
    PlanFormatError,
    PlanInterval,
    from_table,
    interval_minutes,
    newest_by_interval,
    run_sort_key,
    run_time,
)

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
UTC = dt.UTC


def load(name: str, plan_run: str = "2026-01-01T00:00:00Z"):
    return from_table((FIXTURES / f"synthetic_{name}.txt").read_text(), plan_run)


@pytest.fixture
def dst():
    return load("dst_autumn")


class TestTableParsing:
    def test_header_is_parsed_not_assumed(self):
        """The column set has changed before; positional parsing would misalign silently."""
        text = (FIXTURES / "synthetic_self_heavy.txt").read_text()
        shuffled = text.replace("  imp   exp", "  exp   imp")  # header only, data unchanged
        a = from_table(text)[0]
        b = from_table(shuffled)[0]
        assert (a.import_wh, a.export_wh) == (b.export_wh, b.import_wh)

    def test_missing_column_names_itself(self):
        text = (FIXTURES / "synthetic_self_heavy.txt").read_text().replace(" soc ", " sock ")
        with pytest.raises(PlanFormatError, match="missing column"):
            from_table(text)

    def test_unparseable_number_names_the_column_and_line(self):
        lines = (FIXTURES / "synthetic_self_heavy.txt").read_text().splitlines()
        lines[2] = lines[2].replace("15", "NaNe", 1)
        with pytest.raises(PlanFormatError, match="line 3"):
            from_table("\n".join(lines))

    def test_empty_plan_raises(self):
        with pytest.raises(PlanFormatError, match="empty plan"):
            from_table("   \n\n")

    def test_header_without_rows_raises(self):
        head = (FIXTURES / "synthetic_self_heavy.txt").read_text().splitlines()[0]
        with pytest.raises(PlanFormatError, match="no rows"):
            from_table(head + "\n")

    def test_all_starts_are_aware_utc(self):
        for iv in load("self_heavy"):
            assert iv.start.tzinfo is not None
            assert iv.start.utcoffset() == dt.timedelta(0)

    def test_naive_start_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            PlanInterval(dt.datetime(2026, 1, 1), 0, 0, 0, 0, 0)


class TestDstAutumn:
    """2026-10-25: local 02:00-02:59 happens twice, at +02:00 then +01:00.

    `fold` cannot be inferred from a single row -- both passes look identical in isolation --
    so it is resolved from the table's own chronological ordering. Applying fold=0
    unconditionally collapsed the repeated hour onto the first pass AND left 01:00-01:45Z
    with no intervals at all, which the dispatcher reads as a gap: an hour of unplanned
    self-consumption in the middle of the night.
    """

    def test_the_repeated_hour_produces_25_unique_instants(self, dst):
        starts = [iv.start for iv in dst]
        assert len(starts) == len(set(starts)) == 20

    def test_utc_is_strictly_monotonic(self, dst):
        starts = [iv.start for iv in dst]
        assert starts == sorted(starts)

    def test_cadence_stays_15_minutes_across_the_transition(self, dst):
        # The whole point of storing UTC instants: the local clock jumps, the instants do not.
        assert interval_minutes(dst) == 15

    def test_no_utc_hour_is_left_unplanned(self, dst):
        """The regression this fixture exists for."""
        covered = {iv.start for iv in dst}
        t = dt.datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
        while t < dt.datetime(2026, 10, 25, 2, 0, tzinfo=UTC):
            assert t in covered, f"{t.isoformat()} has no interval"
            t += dt.timedelta(minutes=15)

    def test_both_passes_map_to_the_right_offsets(self, dst):
        first = [iv for iv in dst if iv.start == dt.datetime(2026, 10, 25, 0, 0, tzinfo=UTC)]
        second = [iv for iv in dst if iv.start == dt.datetime(2026, 10, 25, 1, 0, tzinfo=UTC)]
        assert len(first) == len(second) == 1
        assert first[0].start.astimezone(PLANNER_TZ).hour == 2   # CEST, +02:00
        assert second[0].start.astimezone(PLANNER_TZ).hour == 2  # CET, +01:00, same local hour

    def test_a_genuinely_out_of_order_table_still_raises(self):
        """fold=1 is tried once. It must not become a licence to accept any ordering."""
        lines = (FIXTURES / "synthetic_self_heavy.txt").read_text().splitlines()
        lines[1], lines[4] = lines[4], lines[1]
        with pytest.raises(PlanFormatError, match="not after the previous row"):
            from_table("\n".join(lines))


class TestNegativePrices:
    """Zero of these in the August archive; common in spring. They invert the logic -- the
    cheapest action is to IMPORT, so the plan charges from the grid at midday."""

    def test_negative_buy_prices_survive_parsing(self):
        ivs = load("negative_prices")
        assert min(i.price_buy for i in ivs) < 0

    def test_the_plan_imports_while_prices_are_negative(self):
        ivs = load("negative_prices")
        negative = [i for i in ivs if i.price_buy < 0]
        assert negative
        assert all(i.charge_wh > 0 for i in negative)


class TestIntervalMinutes:
    def test_needs_two_intervals(self):
        with pytest.raises(PlanFormatError, match="at least two"):
            interval_minutes(load("self_heavy")[:1])

    def test_inconsistent_spacing_lists_the_gaps(self):
        ivs = load("self_heavy")
        with pytest.raises(PlanFormatError, match="inconsistent interval spacing"):
            interval_minutes([ivs[0], ivs[1], ivs[5]])

    def test_implausible_cadence_rejected(self):
        base = load("self_heavy")[0]
        five = [base, PlanInterval(base.start + dt.timedelta(minutes=5), 0, 0, 0, 0, 0)]
        with pytest.raises(PlanFormatError, match="implausible interval"):
            interval_minutes(five)


class TestNewestByInterval:
    def _iv(self, minute: int, run: str, soc: float) -> PlanInterval:
        return PlanInterval(
            start=dt.datetime(2026, 8, 1, 12, minute, tzinfo=UTC),
            soc_wh=soc, charge_wh=0, discharge_wh=0, import_wh=0, export_wh=0, plan_run=run)

    def test_newest_run_wins_for_a_shared_instant(self):
        out = newest_by_interval([
            self._iv(0, "2026-08-01T09:00:00Z", 1000),
            self._iv(0, "2026-08-01T12:00:00Z", 2000),
        ])
        assert [i.soc_wh for i in out] == [2000]

    def test_an_older_run_still_covers_the_tail(self):
        """A newer run with a SHORTER horizon must not leave the tail unplanned."""
        out = newest_by_interval([
            self._iv(0, "2026-08-01T09:00:00Z", 1000),
            self._iv(15, "2026-08-01T09:00:00Z", 1100),
            self._iv(0, "2026-08-01T12:00:00Z", 2000),
        ])
        assert [i.soc_wh for i in out] == [2000, 1100]

    def test_plan_run_is_compared_as_a_timestamp_not_a_string(self):
        """The archive's oldest runs tag themselves `+02:00` and newer ones `Z`.

        As strings, "2026-07-30T17:26:14+02:00" sorts AFTER "2026-08-01T09:00:00Z" on the
        offset character -- picking a run two days stale. Parsed, 15:26Z loses correctly.
        """
        out = newest_by_interval([
            self._iv(0, "2026-07-30T17:26:14+02:00", 1000),
            self._iv(0, "2026-08-01T09:00:00Z", 2000),
        ])
        assert [i.soc_wh for i in out] == [2000]

    def test_unparseable_run_tag_raises(self):
        with pytest.raises(PlanFormatError, match="unparseable plan_run"):
            newest_by_interval([self._iv(0, "not-a-timestamp", 1)])

    def test_output_is_sorted_by_start(self):
        out = newest_by_interval([self._iv(30, "2026-08-01T09:00:00Z", 3), self._iv(0, "2026-08-01T09:00:00Z", 1)])
        assert [i.start.minute for i in out] == [0, 30]


class TestRunSortKey:
    """Ordering `plan_run` tags. The archive genuinely mixes two spellings of an instant:
    everything written before 2026-07-30 carries a `+02:00` offset, everything after ends in
    `Z`, and the corpus still holds both."""

    # 17:26:14+02:00 IS 15:26:14Z -- half an hour EARLIER than 16:00Z, and it sorts after it
    # as a string. This is the pair the whole rule exists for.
    OFFSET = "2026-07-30T17:26:14+02:00"
    LATER_Z = "2026-07-30T16:00:00Z"

    def test_a_string_sort_gets_this_pair_backwards(self):
        """Pins the trap itself, so the fix cannot be reverted as unnecessary. If this ever
        fails, the two spellings stopped disagreeing and the rest of the class is moot."""
        assert sorted([self.OFFSET, self.LATER_Z])[-1] == self.OFFSET
        assert run_time(self.OFFSET) < run_time(self.LATER_Z), (
            "the string sort and the instant must genuinely disagree for this pair")

    def test_the_newest_is_the_later_instant_not_the_later_string(self):
        assert max([self.OFFSET, self.LATER_Z], key=run_sort_key) == self.LATER_Z

    def test_an_unparseable_tag_never_wins_newest(self):
        """`from_table()` is the documented fixture path and labels a plan with its filename,
        so the golden corpus carries tags that are not timestamps at all. They must neither
        raise nor outrank a real run -- a synthetic label winning `plan_run` would put a
        fixture name on the dashboard as the plan in force."""
        assert max([self.LATER_Z, "synthetic_dst_autumn"], key=run_sort_key) == self.LATER_Z

    def test_unparseable_tags_still_order_deterministically(self):
        """Two of them must not compare equal, or the sort is unstable across runs."""
        assert sorted(["synthetic_b", "synthetic_a"], key=run_sort_key) == [
            "synthetic_a", "synthetic_b"]

    # The same instant, spelled both ways the archive spells it. Around the 2026-07-30
    # rollover this pair is not hypothetical.
    SAME_A = "2026-07-30T18:00:00+02:00"
    SAME_Z = "2026-07-30T16:00:00Z"

    def test_two_spellings_of_one_instant_order_deterministically(self):
        """The twin of the test above, for the PARSEABLE branch.

        A key that compares these equal is not a total order, and the two consumers then
        break the tie differently -- `build_document` sorts a set (iteration order varies
        with `PYTHONHASHSEED`), `newest_run` takes the first max of a list. The heartbeat
        would name one run while `slots.json` recorded the other, in some processes only.
        """
        assert run_time(self.SAME_A) == run_time(self.SAME_Z), "this pair must tie on instant"
        assert run_sort_key(self.SAME_A) != run_sort_key(self.SAME_Z), (
            "a tie on the key is the bug: it lets the caller's own iteration order decide")
        forwards = sorted([self.SAME_A, self.SAME_Z], key=run_sort_key)
        backwards = sorted([self.SAME_Z, self.SAME_A], key=run_sort_key)
        assert forwards == backwards, "the order must not depend on the order they arrived in"
        assert max([self.SAME_A, self.SAME_Z], key=run_sort_key) == forwards[-1], (
            "`max()` over a list and `sorted()` over a set must pick the SAME tag -- "
            "`newest_run` uses one and `build_document` the other")


class TestTagsWithNoOffset:
    """A `plan_run` with no UTC offset is malformed, not a tag to guess the zone of.

    Both spellings the archive contains carry an offset. Guessing costs two hours -- eight
    intervals -- and `datetime.fromisoformat` accepts the naive form happily, so without
    this the value flows on and detonates as a `TypeError` inside some later `sorted()`.
    """

    NAIVE = "2026-08-01T10:00:00"

    def test_run_time_rejects_it_rather_than_assuming_a_zone(self):
        with pytest.raises(PlanFormatError, match="no UTC offset"):
            run_time(self.NAIVE)

    def test_it_sorts_as_malformed_instead_of_raising_typeerror(self):
        """It must lose to a real run, the same way any other bad tag does."""
        assert max(["2026-08-01T09:00:00Z", self.NAIVE], key=run_sort_key) == \
            "2026-08-01T09:00:00Z"

    def test_newest_by_interval_raises_on_it(self):
        """Consistent with `test_unparseable_run_tag_raises`: that path does not swallow
        bad tags, and a naive one is no more usable than `not-a-timestamp`."""
        iv = PlanInterval(
            start=dt.datetime(2026, 8, 1, 9, tzinfo=UTC), soc_wh=1000.0,
            charge_wh=0, discharge_wh=0, import_wh=0, export_wh=0, plan_run=self.NAIVE)
        with pytest.raises(PlanFormatError, match="no UTC offset"):
            newest_by_interval([iv])
