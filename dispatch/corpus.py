"""On-disk snapshots of real plan runs. PLAN-repo-seams.md section 5c.

The corpus exists so tests and review charts never depend on the NAS being reachable, and so
that "what did run X translate to" is answerable without a database. One file per plan run,
in `dispatch/testdata/`, written by `scripts/fetch-plan-corpus.py`.

PRIVACY. This directory is gitignored and must stay that way -- verify with
`git check-ignore -v` before any fetch, because this repo is public. Two notes on what is
actually in these files:

  - `load_forecast_wh` is NOT here. `PlanInterval` never carried it, so it is dropped at the
    parse boundary rather than filtered out later. That is the strongest form of the
    protection: the household's 15-minute occupancy signal is not in the shape at all.
  - What remains is still a household's SoC trajectory and PV forecast. Less sensitive, not
    nothing. The gitignore stays regardless.

FORMAT. JSON, one object per run, `schema` versioned. Deliberately not pickle and not the
planner's table format: it must be diffable when a fetch is re-run, and readable by a human
deciding whether a run is worth reviewing.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from plan import PlanFormatError, PlanInterval, run_time
from plan import iso_z as _iso

SCHEMA = 1

# Where the corpus lives, relative to the repo root. Resolved from this file rather than the
# working directory, because the fetch script, pytest and the chart generator all run from
# different places.
TESTDATA_DIR = Path(__file__).resolve().parent / "testdata"

_FLOAT_FIELDS = (
    "soc_wh", "charge_wh", "discharge_wh", "import_wh", "export_wh",
    "price_buy", "price_sell", "cost_eur", "pv_forecast_wh",
)


def run_filename(plan_run: str) -> str:
    """A filesystem-safe name for a run tag.

    `plan_run` is an ISO timestamp and the older ones carry a `+02:00` offset, so colons and
    plus signs both appear. Both are legal on this filesystem and neither is legal everywhere,
    and a corpus that only unpacks on macOS would be a poor test fixture.
    """
    safe = plan_run.replace(":", "").replace("+", "p").replace("-", "")
    return f"run_{safe}.json"


def dump_run(intervals: list[PlanInterval], features: dict, path: Path) -> None:
    """Write one run's intervals plus the features that got it selected.

    The features travel with the data on purpose: six months from now the question about a
    fixture is always "why is this one in here", and the answer should not require re-running
    the selection.
    """
    if not intervals:
        raise PlanFormatError("refusing to write an empty run")
    runs = {iv.plan_run for iv in intervals}
    if len(runs) != 1:
        raise PlanFormatError(f"a corpus file holds exactly one plan run, got {sorted(runs)}")

    payload = {
        "schema": SCHEMA,
        "plan_run": intervals[0].plan_run,
        "fetched_at": _iso(dt.datetime.now(dt.UTC)),
        "interval_count": len(intervals),
        "features": features,
        "intervals": [
            {"start": _iso(iv.start), **{f: getattr(iv, f) for f in _FLOAT_FIELDS}}
            for iv in sorted(intervals, key=lambda i: i.start)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))


def load_run(path: Path) -> tuple[list[PlanInterval], dict]:
    """Read one corpus file back. Returns (intervals, metadata-without-intervals)."""
    try:
        doc = json.loads(Path(path).read_text())
    except FileNotFoundError as e:
        raise PlanFormatError(
            f"{path} is missing -- run scripts/fetch-plan-corpus.py to build the corpus") from e
    except json.JSONDecodeError as e:
        raise PlanFormatError(f"{path} is not valid JSON: {e}") from e

    if doc.get("schema") != SCHEMA:
        raise PlanFormatError(
            f"{path} is schema {doc.get('schema')}, this code reads {SCHEMA} -- re-fetch")

    plan_run = doc["plan_run"]
    intervals = []
    for i, row in enumerate(doc["intervals"]):
        try:
            start = dt.datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            intervals.append(PlanInterval(
                start=start, plan_run=plan_run,
                **{f: float(row[f]) for f in _FLOAT_FIELDS}))
        except (KeyError, ValueError) as e:
            raise PlanFormatError(f"{path} interval {i}: {e}") from e

    meta = {k: v for k, v in doc.items() if k != "intervals"}
    return intervals, meta


def load_all(directory: Path | None = None) -> list[tuple[list[PlanInterval], dict]]:
    """Every corpus file, oldest run first. Empty list when the corpus has not been fetched.

    Returning empty rather than raising lets tests skip cleanly on a machine that has never
    talked to the NAS, which is the normal state for CI.
    """
    d = Path(directory or TESTDATA_DIR)
    if not d.is_dir():
        return []
    loaded = [load_run(p) for p in sorted(d.glob("run_*.json"))]
    # Ordered by PARSED tag, never by the string. The archive's oldest runs carry `+02:00`
    # and the rest carry `Z`, so a lexical sort silently interleaves them -- and this order
    # is what names the pytest fixture ids and lays out the HTML review page, so getting it
    # wrong means a reviewer reads runs out of sequence without anything looking wrong.
    return sorted(loaded, key=lambda pair: run_time(pair[1]["plan_run"]))
