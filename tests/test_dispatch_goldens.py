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


@pytest.mark.skipif(not REGENERATE, reason="regeneration only")
def test_regenerate_reviewed_runs():
    """Rewrites `reviewed_runs.json` from the local corpus. Never runs in a normal pytest.

    Reasons come from the corpus manifest, which is what recorded why each run was selected in
    the first place; a run already in the golden file keeps its existing reason if the manifest
    no longer names it, so re-fetching a newer archive cannot erase the review history.
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

    runs = []
    for plan_run in reasons:
        intervals = by_run.get(plan_run)
        if intervals is None:
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

    REVIEWED.parent.mkdir(parents=True, exist_ok=True)
    REVIEWED.write_text(json.dumps({
        "reviewed_on": existing.get("reviewed_on", "2026-08-16"),
        "capacity_wh": CAPACITY_WH,
        "generated_at": GENERATED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": sorted(runs, key=lambda r: r["plan_run"]),
    }, indent=1) + "\n")
