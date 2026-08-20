"""Invariants over the whole real plan archive. PLAN-repo-seams.md section 5d.

These need no golden and no human, so they run over EVERY run in the corpus rather than the
eight selected for review. That is the point of keeping the full archive: the reviewed runs
answer "is this right", the sweep answers "is it still right for the other 129".

The corpus is fetched by `scripts/fetch-plan-corpus.py` into `dispatch/testdata/`, which is
gitignored -- it derives from household plan data and this repo is public. So these tests
SKIP rather than fail when it is absent, which is the normal state in CI and on any machine
that has never talked to the NAS. Everything that must hold without a corpus is covered by
the synthetic fixtures in test_dispatch_translator.py.
"""
from __future__ import annotations

import datetime as dt

import pytest

import corpus
from plan import interval_minutes, newest_by_interval, run_time
from translator import ENERGY_FLOOR_WH, build_document, classify, to_slots

UTC = dt.UTC

# A sanity ceiling on what the PLAN may ask for, sitting just above the planner's tuned
# maxChargeSpeed=4850 / maxDischargeSpeed=4700. A slot exceeding this is not "the inverter is
# the limit" -- it is a plan asking for something the battery cannot physically do.
#
# Deliberately not read from the inverter, and deliberately not `slots.HARD_MAX_POWER_W`. The
# registers overstate the hardware threefold (15,015 / 13,728 W, measured 2026-08-16), and
# `clamp()` is the layer that copes with a plan being wrong. This sweep asserts the plans
# themselves are not, which is a separate claim and worth its own number.
INVERTER_MAX_W = 6000

CORPUS = corpus.load_all()
CAPACITY_WH = 27900.0

pytestmark = pytest.mark.skipif(
    not CORPUS,
    reason="no plan corpus -- run scripts/fetch-plan-corpus.py (needs the NAS)")


def _ids():
    return [meta["plan_run"] for _, meta in CORPUS]


@pytest.fixture(params=range(len(CORPUS)), ids=_ids() or None, scope="module")
def run(request):
    """One archived plan run: (intervals, metadata, slots, warnings).

    Module-scoped because the translation is the expensive part and every test below only
    reads it. Function-scoped, each archived run was re-translated once per dependent test
    -- around fifteen times per run, across the whole archive, for one distinct result.
    """
    intervals, meta = CORPUS[request.param]
    slots, warnings = to_slots(intervals, CAPACITY_WH)
    return intervals, meta, slots, warnings


@pytest.fixture(scope="module")
def document(run):
    """The same run as a slots document. Separate fixture so `TestDocumentContract` builds
    it once rather than once per assertion about it."""
    intervals, _meta, _slots, _warnings = run
    generated = dt.datetime.now(UTC)
    doc, _ = build_document(intervals, CAPACITY_WH, generated_at=generated)
    return doc, generated


class TestEveryRunTranslates:
    def test_it_produces_slots_without_raising(self, run):
        _, _, slots, _ = run
        assert slots

    def test_no_warnings_anywhere_in_the_archive(self, run):
        """The direction guard's plan_run fix is what made this hold.

        Before it, six runs warned -- every one exactly on a 3-hourly run boundary, where the
        outgoing run's projection was being compared against the incoming run's re-anchored
        SoC. If this starts failing, look at the seam before looking at the arithmetic.
        """
        _, meta, _, warnings = run
        assert warnings == [], f"{meta['plan_run']}: {warnings}"

    def test_every_action_is_one_the_dispatcher_understands(self, run):
        _, _, slots, _ = run
        assert {s.action for s in slots} <= {"charge", "discharge", "self", "hold"}


class TestSlotGeometry:
    def test_slots_are_contiguous(self, run):
        _, meta, slots, _ = run
        for a, b in zip(slots, slots[1:]):
            assert a.end == b.start, f"{meta['plan_run']}: gap/overlap at {a.end.isoformat()}"

    def test_slots_never_overlap(self, run):
        _, _, slots, _ = run
        for a, b in zip(slots, slots[1:]):
            assert a.start < a.end <= b.start

    def test_slots_cover_the_plan_exactly(self, run):
        intervals, _, slots, _ = run
        ivs = sorted(intervals, key=lambda i: i.start)
        span = dt.timedelta(minutes=interval_minutes(ivs))
        assert slots[0].start == ivs[0].start
        assert slots[-1].end == ivs[-1].start + span

    def test_all_timestamps_are_utc_instants(self, run):
        _, _, slots, _ = run
        for s in slots:
            assert s.start.utcoffset() == dt.timedelta(0)
            assert s.end.utcoffset() == dt.timedelta(0)


class TestCommandShape:
    def test_command_slots_carry_power_and_target(self, run):
        _, _, slots, _ = run
        for s in slots:
            if s.action in ("charge", "discharge"):
                assert s.power_w is not None and s.target_soc is not None

    def test_non_command_slots_carry_neither(self, run):
        """`self` and `hold` are the absence of a setpoint, not a zero one."""
        _, _, slots, _ = run
        for s in slots:
            if s.action in ("self", "hold"):
                assert s.power_w is None and s.target_soc is None

    def test_power_is_a_positive_magnitude(self, run):
        """`Slot.power_w` is unsigned -- the action carries the direction. The sign is
        applied once, at the dispatcher."""
        _, _, slots, _ = run
        for s in slots:
            if s.power_w is not None:
                assert s.power_w > 0

    def test_power_is_within_the_inverter_limits(self, run):
        _, meta, slots, _ = run
        for s in slots:
            if s.power_w is not None:
                assert s.power_w <= INVERTER_MAX_W, (
                    f"{meta['plan_run']} {s.start.isoformat()}: {s.power_w} W")

    def test_targets_are_a_valid_percentage(self, run):
        _, _, slots, _ = run
        for s in slots:
            if s.target_soc is not None:
                assert 0.0 <= s.target_soc <= 100.0


class TestDirectionRule:
    """The invariant that makes a command do anything at all: a Mode 2 target on the wrong
    side of the starting SoC is accepted by the inverter and silently ignored."""

    def test_every_emitted_command_moves_soc_in_its_own_direction(self, run):
        intervals, meta, slots, _ = run
        ivs = sorted(intervals, key=lambda i: i.start)
        soc_at_end = {i.start: 100.0 * i.soc_wh / CAPACITY_WH for i in ivs}
        starts = sorted(soc_at_end)

        for s in slots:
            if s.action not in ("charge", "discharge"):
                continue
            pos = starts.index(s.start)
            if pos == 0:
                continue
            start_soc = soc_at_end[starts[pos - 1]]
            # Only meaningful within one run; across the 3-hourly seam the previous run's
            # projection is a stale forecast, not a measurement.
            if ivs[pos].plan_run != ivs[pos - 1].plan_run:
                continue
            if s.action == "charge":
                assert s.target_soc > start_soc, f"{meta['plan_run']} {s.start.isoformat()}"
            else:
                assert s.target_soc < start_soc, f"{meta['plan_run']} {s.start.isoformat()}"


class TestEnergyReconciles:
    def test_slot_power_matches_the_plans_own_energy(self, run):
        """Wh -> W is the one arithmetic step here, and getting the cadence wrong scales
        every setpoint by 4x while still looking plausible."""
        intervals, meta, slots, _ = run
        ivs = sorted(intervals, key=lambda i: i.start)
        span = dt.timedelta(minutes=interval_minutes(ivs))
        per_hour = 3600.0 / span.total_seconds()

        by_start = {i.start: i for i in ivs}
        for s in slots:
            if s.action not in ("charge", "discharge"):
                continue
            covered, t = [], s.start
            while t < s.end:
                covered.append(by_start[t])
                t += span
            wh = sum(i.charge_wh if s.action == "charge" else i.discharge_wh for i in covered)
            expected = round(wh * per_hour / len(covered))
            assert abs(s.power_w - expected) <= 1, (
                f"{meta['plan_run']} {s.start.isoformat()}: {s.power_w} vs {expected}")

    def test_commanded_energy_never_exceeds_the_plans(self, run):
        """Downgrades only ever remove dispatch, never add it."""
        intervals, _, slots, _ = run
        ivs = sorted(intervals, key=lambda i: i.start)
        span = dt.timedelta(minutes=interval_minutes(ivs))
        hours = span.total_seconds() / 3600.0

        for action, field in (("charge", "charge_wh"), ("discharge", "discharge_wh")):
            matching = [s for s in slots if s.action == action]
            commanded = sum(
                s.power_w * (s.end - s.start).total_seconds() / 3600.0 for s in matching)
            planned = sum(getattr(i, field) for i in ivs)
            # The allowance SCALES with the number of slots, because the error does: each
            # slot's power is rounded to a whole watt, so each contributes at most
            # 0.5 W x its own hours. A fixed one-interval allowance happened to hold over
            # the 136 runs in the archive and would start failing on a longer horizon or a
            # finer interval -- as a CI flake on a corpus re-fetch, months from the change
            # that caused it. Still tight: at 15-minute slots this is 0.25 Wh apiece
            # against plans of several kWh.
            tolerance = hours * max(1, len(matching))
            assert commanded <= planned + tolerance, (action, commanded, planned, tolerance)


class TestDocumentContract:
    def test_no_slot_cites_a_run_newer_than_generated_at(self, document):
        doc, generated = document
        for tag in doc["plan_runs"]:
            assert run_time(tag) <= generated

    def test_horizon_end_matches_the_last_slot(self, document):
        doc, _ = document
        assert doc["horizon_end"] == doc["slots"][-1]["end"]

    def test_the_document_is_json_serialisable(self, document):
        import json
        doc, _ = document
        assert json.loads(json.dumps(doc))["slots"]


class TestArchiveWide:
    """Properties of the corpus as a whole, not of any single run."""

    def test_the_corpus_contains_more_than_one_run(self):
        assert len(CORPUS) > 1

    def test_overlapping_runs_resolve_to_one_interval_per_instant(self):
        """Section 3.3's newest-wins rule, exercised by ordinary data.

        Over ~30 h horizons every instant is covered by roughly a dozen runs at the 3-hourly
        cadence, and by ~30 at the hourly one the planner moved to -- so this needs no
        contrived fixture, only the archive.
        """
        every = [i for ivs, _meta in CORPUS for i in ivs]
        resolved = newest_by_interval(every)
        starts = [i.start for i in resolved]
        assert len(starts) == len(set(starts))
        assert starts == sorted(starts)

    def test_the_archive_still_contains_no_charge_and_discharge_interval(self):
        """`classify()`'s BOTH branch is dead against real data -- 0 of ~13,000 intervals.

        Pinned here so that if the LP ever starts emitting one, this says so rather than the
        translator raising in production. The branch itself is covered by a hand-written case
        in test_dispatch_translator.py.
        """
        for ivs, meta in CORPUS:
            for i in ivs:
                both = i.charge_wh > ENERGY_FLOOR_WH and i.discharge_wh > ENERGY_FLOOR_WH
                assert not both, f"{meta['plan_run']} {i.start.isoformat()}"

    def test_both_plan_run_timestamp_formats_are_present(self):
        """The oldest runs tag `+02:00`, newer ones `Z`. A corpus with only one format would
        not exercise the parse-don't-string-sort rule in `newest_by_interval()`."""
        formats = {"offset" if "+" in meta["plan_run"] else "utc" for _, meta in CORPUS}
        assert formats == {"offset", "utc"}, f"corpus only has {formats}"

    def test_every_action_occurs_somewhere_in_the_archive(self):
        seen = {classify(i) for ivs, _meta in CORPUS for i in ivs}
        assert {"charge", "discharge", "hold", "self"} <= seen
