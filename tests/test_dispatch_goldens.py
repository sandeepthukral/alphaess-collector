"""Layer A: plan -> slots, frozen. PLAN-repo-seams.md section 5d.

Goldens exist so that a change to `classify()` or `to_slots()` cannot quietly alter what the
inverter is told to do. They come in two shapes here, and the split is about privacy rather
than taste:

  - **Synthetic plans** (`tests/fixtures/plans/synthetic_*.txt`) carry invented load and PV
    numbers, so their slot documents are committed in full and read like documentation. These
    are the cases the real archive cannot contain -- a DST night, negative prices, a
    `self`-heavy day -- and they are the only golden coverage available in CI.

  - **Reviewed real runs** are committed as a DIGEST plus action counts, never as the document.
    A slot document carries `target_soc` every interval, and differencing that trajectory
    recovers the household's consumption at 15-minute resolution -- the same signal
    `dispatch/corpus.py` refuses to keep, arriving by another route. This repo is public. The
    digest still fails loudly on any change; reading WHAT changed is done locally, against the
    gitignored corpus, which is where the plan data lives anyway.

The nine reviewed runs are the ones `scripts/fetch-plan-corpus.py` selected and a human then
checked chart by chart on 2026-08-16. `reason` travels with each entry so the next person does
not have to re-derive why a particular run is being defended.

Regenerate with `REGENERATE_GOLDEN=1 pytest tests/test_dispatch_goldens.py`, following
`battery-planning/tests/test_golden_plan.py`'s convention. Regenerating the reviewed-run
digests needs the corpus on disk; regenerating the synthetics does not.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import ClassVar

import pytest

import corpus
from plan import from_table
from translator import build_document

FIXTURES = Path(__file__).parent / "fixtures"
PLANS_DIR = FIXTURES / "plans"
GOLDEN_DIR = FIXTURES / "golden_slots"
REVIEWED = GOLDEN_DIR / "reviewed_runs.json"

CAPACITY_WH = 27900.0

# Fixed so the documents are a pure function of the plan. `build_document` takes this as an
# argument for exactly this reason -- see its docstring.
GENERATED_AT = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.UTC)

REGENERATE = os.environ.get("REGENERATE_GOLDEN") == "1"

CORPUS = corpus.load_all()

SYNTHETICS = sorted(p.stem for p in PLANS_DIR.glob("synthetic_*.txt"))


def _translate(intervals: list) -> tuple[dict, list[str]]:
    return build_document(intervals, CAPACITY_WH, GENERATED_AT)


def _golden_shape(doc: dict, warnings: list[str]) -> dict:
    """What gets written to a synthetic golden file.

    The document is nested rather than merged with the warnings, so `document` stays byte-for-
    byte what the dispatcher would read as `slots.json`. A golden that is almost-but-not-quite
    the real contract is a trap for whoever copies one to reproduce a bug.
    """
    return {"warnings": warnings, "document": doc}


def _digest(doc: dict) -> str:
    """A stable fingerprint of everything except when it was generated.

    Separators and `sort_keys` are pinned because the digest is committed: a change in how
    Python happens to serialise a dict must not read as a change in dispatch behaviour.
    """
    body = {k: v for k, v in doc.items() if k != "generated_at"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _actions(doc: dict) -> dict[str, int]:
    return dict(sorted(Counter(s["action"] for s in doc["slots"]).items()))


class TestSyntheticGoldens:
    """The three cases the August archive cannot contain. Committed in full."""

    @pytest.mark.parametrize("name", SYNTHETICS)
    def test_plan_translates_to_its_golden_document(self, name):
        intervals = from_table((PLANS_DIR / f"{name}.txt").read_text(), plan_run=name)
        doc, warnings = _translate(intervals)
        shape = _golden_shape(doc, warnings)
        path = GOLDEN_DIR / f"{name}.json"

        if REGENERATE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(shape, indent=1) + "\n")

        assert path.exists(), f"{path} is missing -- REGENERATE_GOLDEN=1 to write it"
        assert json.loads(path.read_text()) == shape

    def test_every_synthetic_fixture_has_a_golden(self):
        """A new fixture with no golden would otherwise sit there testing nothing."""
        assert SYNTHETICS, "the synthetic plan fixtures have gone missing"
        for name in SYNTHETICS:
            assert (GOLDEN_DIR / f"{name}.json").exists(), f"no golden for {name}"


class TestReviewedRuns:
    """The nine real runs a human signed off on. Digests only -- see the module docstring."""

    def _golden(self) -> dict:
        return json.loads(REVIEWED.read_text())

    def _by_run(self) -> dict[str, list]:
        return {meta["plan_run"]: ivs for ivs, meta in CORPUS}

    @pytest.mark.skipif(not CORPUS, reason="no plan corpus -- run scripts/fetch-plan-corpus.py")
    def test_reviewed_runs_translate_as_reviewed(self):
        by_run = self._by_run()
        golden = self._golden()
        checked = 0

        for entry in golden["runs"]:
            intervals = by_run.get(entry["plan_run"])
            if intervals is None:
                # A partially re-fetched corpus is normal. Silence here is deliberate: the
                # test that the LIST is intact is separate, and lives below.
                continue
            doc, warnings = _translate(intervals)
            run = entry["plan_run"]
            # Counts first. When something does change, `charge 12 -> 11` is a usable failure
            # and a digest mismatch on its own is not.
            assert _actions(doc) == entry["actions"], f"{run}: action mix changed"
            assert len(doc["slots"]) == entry["slots"], f"{run}: slot count changed"
            assert warnings == entry["warnings"], f"{run}: warnings changed"
            assert _digest(doc) == entry["digest"], (
                f"{run}: slot document changed in a way the counts do not show -- diff it "
                f"locally against dispatch/testdata/, this is the reviewed output")
            checked += 1

        if not checked:
            pytest.skip("none of the reviewed runs are in the local corpus")

    def test_the_reviewed_list_is_intact(self):
        """Runs a human checked. Dropping one silently would undo the review."""
        golden = self._golden()
        assert golden["capacity_wh"] == CAPACITY_WH
        runs = [e["plan_run"] for e in golden["runs"]]
        assert len(runs) == len(set(runs)), "duplicate plan_run in the reviewed list"
        assert len(runs) == 9, "the 2026-08-16 review covered nine runs"
        for entry in golden["runs"]:
            assert entry["reason"], f"{entry['plan_run']} has no reason recorded"
            assert len(entry["digest"]) == 64


def _rebuild_runs(reasons: dict, by_run: dict, entries: dict,
                  prev_header: dict | None = None) -> tuple[list[dict], list[str]]:
    """One reviewed-runs list, rebuilt from whatever corpus is on disk.

    Split out of the regeneration test so the carry-through rule is reachable without
    REGENERATE=1 and without rewriting the committed file. The rule is the whole reason this
    function is worth having: a run that cannot be re-derived keeps its reviewed entry.

    Returns the runs AND the orphans -- manifest names it, corpus lacks it, nobody reviewed
    it. The orphans are returned rather than printed because a `print` inside a passing test
    is swallowed by pytest's capture: the operator running the documented regeneration
    command would never see it, which is the opposite of loud.
    """
    runs, orphans = [], []
    for plan_run in reasons:
        intervals = by_run.get(plan_run)
        if intervals is None:
            # NOT `continue`. A reviewed run missing from the local corpus is the ordinary
            # case after re-fetching -- the corpus is gitignored and selection is by shape, so
            # any refetch can return a different set -- and dropping it here would delete a
            # human's review from the only file that records it. The entry still carries its
            # reason, digest and counts from the last regeneration, so it is carried through
            # verbatim. This is what the docstring below promises; it used to `continue`.
            if plan_run in entries:
                # Stamped with the header its digest was actually derived under, because the
                # file's own header is re-written from TODAY's constants on every
                # regeneration. After a CAPACITY_WH change the header would otherwise claim a
                # capacity that is true for the re-derived entries and false for these, and
                # the mismatch would surface much later as an unexplained digest failure.
                carried = dict(entries[plan_run])
                if prev_header:
                    carried.setdefault("carried_from", {
                        "capacity_wh": prev_header.get("capacity_wh"),
                        "generated_at": prev_header.get("generated_at"),
                    })
                runs.append(carried)
            else:
                # Named by the manifest, absent from the corpus, never reviewed. Nothing to
                # preserve and nothing to derive, but it must not pass silently: it means the
                # manifest and the corpus disagree about what was fetched.
                orphans.append(plan_run)
            continue
        doc, warnings = _translate(intervals)
        runs.append({
            "plan_run": plan_run,
            "reason": reasons[plan_run],
            "slots": len(doc["slots"]),
            "actions": _actions(doc),
            "warnings": warnings,
            "digest": _digest(doc),
        })
    return runs, orphans


class TestReviewHistorySurvivesRegeneration:
    """`reviewed_runs.json` is the only record that a human looked at a run and accepted its
    output. `dispatch/testdata/` is gitignored, so the corpus it was derived from may simply
    not be on the next machine -- which makes "regenerate against a partial corpus" the normal
    case rather than the exotic one."""

    ENTRY: ClassVar[dict] = {
        "plan_run": "2026-08-01T09:00:00Z",
        "reason": "deepest discharge cycle in the window",
        "slots": 42, "actions": {"discharge": 8}, "warnings": [], "digest": "a" * 64,
    }

    def test_a_reviewed_run_absent_from_the_corpus_is_carried_through(self):
        """The regression. `continue` here deleted the reason, the digest and the fact that
        anybody had ever reviewed it -- from the only file that knows."""
        out, orphans = _rebuild_runs(
            {self.ENTRY["plan_run"]: self.ENTRY["reason"]}, {}, {self.ENTRY["plan_run"]: self.ENTRY})
        assert out == [self.ENTRY], "the reviewed entry was dropped, not carried through"
        assert orphans == []

    def test_it_is_carried_through_verbatim(self):
        """Not rebuilt from the reason alone: the digest is the reviewed artefact, and
        inventing a fresh one would silently re-bless output nobody looked at."""
        out, _ = _rebuild_runs(
            {self.ENTRY["plan_run"]: "a different reason now"}, {},
            {self.ENTRY["plan_run"]: self.ENTRY})
        assert out[0]["digest"] == self.ENTRY["digest"]
        assert out[0]["reason"] == self.ENTRY["reason"]

    def test_a_carried_entry_records_the_header_its_digest_came_from(self):
        """The file's header is re-stamped from today's constants on every regeneration, but
        a carried digest was computed under the old ones. Without this, a CAPACITY_WH change
        leaves the header asserting a capacity that is false for exactly these entries."""
        out, _ = _rebuild_runs(
            {self.ENTRY["plan_run"]: self.ENTRY["reason"]}, {},
            {self.ENTRY["plan_run"]: self.ENTRY},
            {"capacity_wh": 27900.0, "generated_at": "2026-08-16T12:00:00Z"})
        assert out[0]["carried_from"] == {
            "capacity_wh": 27900.0, "generated_at": "2026-08-16T12:00:00Z"}

    def test_an_unreviewable_unknown_run_is_returned_as_an_orphan(self):
        """Manifest names it, corpus lacks it, nobody reviewed it. Nothing to carry, so it is
        dropped -- but reported, because it means the two disagree about what was fetched.

        Returned rather than printed: pytest captures and discards stdout for a PASSING test,
        so the `print` this replaced was invisible in the one command that triggers it.
        """
        out, orphans = _rebuild_runs({"2026-08-09T12:00:00Z": "some reason"}, {}, {})
        assert out == []
        assert orphans == ["2026-08-09T12:00:00Z"]

    def test_a_run_that_can_be_rebuilt_is_rebuilt_rather_than_carried(self):
        """The carry-through must not shadow real regeneration, or the file would freeze.

        Deliberately corpus-FREE. This is the guard against the failure mode the carry-through
        fix could itself introduce, so it has to be green on every PR -- and the gitignored
        corpus is absent in CI. A committed synthetic plan exercises the same branch.
        """
        ivs = from_table((PLANS_DIR / "synthetic_dst_autumn.txt").read_text(), plan_run="run-a")
        stale = dict(self.ENTRY, plan_run="run-a", digest="b" * 64, slots=42)
        out, orphans = _rebuild_runs({"run-a": "why"}, {"run-a": ivs}, {"run-a": stale})
        assert orphans == []
        # Pins that the re-derivation is the REAL one, not merely different from the stale
        # placeholder -- which any non-"bbbb..." string would have satisfied.
        assert out[0]["digest"] == _digest(_translate(ivs)[0])
        assert out[0]["reason"] == "why"
        assert out[0]["slots"] != stale["slots"]
        assert "carried_from" not in out[0], "a re-derived entry is not a carried one"

    @pytest.mark.skipif(not CORPUS, reason="no plan corpus on this machine")
    def test_the_same_holds_for_a_real_corpus_run(self):
        """The synthetic above is the one that must stay green in CI; this repeats it against
        real archive data when that is available locally."""
        ivs, meta = CORPUS[0]
        stale = dict(self.ENTRY, plan_run=meta["plan_run"], digest="b" * 64)
        out, _ = _rebuild_runs({meta["plan_run"]: "why"}, {meta["plan_run"]: ivs},
                               {meta["plan_run"]: stale})
        assert out[0]["digest"] == _digest(_translate(ivs)[0])
        assert out[0]["reason"] == "why"


@pytest.mark.skipif(not REGENERATE, reason="regeneration only")
def test_regenerate_reviewed_runs():
    """Rewrites `reviewed_runs.json` from the local corpus. Never runs in a normal pytest.

    Reasons come from the corpus manifest, which is what recorded why each run was selected in
    the first place; a run already in the golden file keeps its existing reason if the manifest
    no longer names it, so re-fetching a newer archive cannot erase the review history.

    That promise covers the run being absent from the CORPUS too, not just from the manifest.
    `dispatch/testdata/` is gitignored and selection is by shape rather than recency, so a
    refetch legitimately returns a different set of runs -- and `reviewed_runs.json` is the
    only place a human's review of a run is recorded. A reviewed run that cannot be
    re-derived is therefore carried through unchanged rather than dropped.
    """
    if not CORPUS:
        pytest.skip("no corpus to regenerate from")

    by_run = {meta["plan_run"]: ivs for ivs, meta in CORPUS}
    existing = json.loads(REVIEWED.read_text()) if REVIEWED.exists() else {"runs": []}
    reasons = {e["plan_run"]: e["reason"] for e in existing["runs"]}
    manifest_path = corpus.TESTDATA_DIR / "manifest.json"
    if manifest_path.exists():
        for sel in json.loads(manifest_path.read_text()).get("selected", []):
            reasons.setdefault(sel["plan_run"], sel["reason"])

    runs, orphans = _rebuild_runs(
        reasons, by_run, {e["plan_run"]: e for e in existing["runs"]}, existing)

    REVIEWED.parent.mkdir(parents=True, exist_ok=True)
    REVIEWED.write_text(json.dumps({
        "reviewed_on": existing.get("reviewed_on", "2026-08-16"),
        "capacity_wh": CAPACITY_WH,
        "generated_at": GENERATED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": sorted(runs, key=lambda r: r["plan_run"]),
    }, indent=1) + "\n")

    # AFTER the write, so a disagreement never costs the regeneration itself -- but as a
    # failed assertion rather than a print, which pytest would swallow on a passing test.
    assert not orphans, (
        f"the manifest names run(s) absent from the corpus, with no reviewed entry to carry "
        f"forward: {', '.join(orphans)}. The file was still written; re-fetch the corpus if "
        f"these were meant to be included.")
