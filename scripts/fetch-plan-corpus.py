#!/usr/bin/env python3
"""Snapshot the real plan archive from InfluxDB into `dispatch/testdata/`.

PLAN-repo-seams.md section 5c. Run from a machine that can reach the NAS; the resulting
corpus is what the tests and review charts use, so they never need the database.

    scripts/fetch-plan-corpus.py --survey          # print the archive, write nothing
    scripts/fetch-plan-corpus.py                   # fetch everything, mark ~8 for review

WHY INFLUX AND NOT THE PLAN FILES ON THE NAS. It is the production code path -- the same
`plan.from_influx()` the dispatcher runs -- it needs no SSH, and only it carries the
`plan_run` tag. With ~36 h horizons each instant is covered by a dozen runs at the 3-hourly
cadence the older archive was written at, and by ~36 since the planner went hourly, so section
3.3's newest-wins rule is exercised by ordinary data instead of a contrived fixture.

WHY EVERY RUN IS WRITTEN, BUT ONLY ~8 ARE MARKED. Two different jobs:

  - the INVARIANT sweep (direction rule, contiguity, power limits, energy reconciliation)
    needs no human and gets more valuable the more runs it sees, so it runs over all of them
  - the GOLDEN review needs a person to look at a chart and agree it is right, which does not
    scale past what fits in one sitting

Selection for the second is BY SHAPE, NOT RECENCY. The August window is homogeneous; taking
the eight newest runs would produce eight near-identical fixtures and a test suite that
notices nothing.

PRIVACY. `dispatch/testdata/` must be gitignored before the first run -- this repo is public.
Checked below, and the script refuses to write if it is not. See dispatch/corpus.py on what
these files do and do not contain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dispatch"))

import corpus  # noqa: E402
from plan import PlanFormatError, from_influx, interval_minutes, iso_z, run_time  # noqa: E402
from translator import ENERGY_FLOOR_WH, classify  # noqa: E402

DEFAULT_CAPACITY_WH = 27900.0


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"{name} is not set (source .env, or pass the matching flag)")
    return v


def assert_gitignored(path: Path) -> None:
    """Refuse to write household data into a public repo.

    A check rather than a comment because the entry has been removed once already -- it was
    deleted when the translator was still going to live in the other repo -- and the cost of
    getting this wrong is a public commit of a household's plan archive.
    """
    probe = path / "probe.json"
    r = subprocess.run(["git", "check-ignore", "-v", str(probe)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(
            f"REFUSING TO WRITE: {path.relative_to(REPO)} is not gitignored, and this repo is "
            f"public.\nAdd `dispatch/testdata/` to .gitignore and re-run.")
    print(f"gitignore ok: {r.stdout.strip()}")


def features_of(intervals: list, capacity_wh: float, floor: float) -> dict:
    """Everything selection needs, computed once per run.

    Stored alongside the intervals by `corpus.dump_run`, so a fixture can always explain why
    it is in the corpus without re-running the selection.
    """
    ivs = sorted(intervals, key=lambda i: i.start)
    # `capacity_wh` is passed because the translator passes it. Without it the harvest rule
    # is off, so `self` is undercounted here while the real translation counts it -- and
    # `self` coverage is the first thing selection ranks on, so scoring on the wrong number
    # picks the wrong runs and the printed action mix disagrees with the goldens.
    actions = [classify(iv, floor, capacity_wh=capacity_wh) for iv in ivs]
    counts: dict[str, int] = defaultdict(int)
    for a in actions:
        counts[a] += 1

    self_starts = [
        iso_z(iv.start)
        for iv, a in zip(ivs, actions) if a == "self"
    ]
    socs = [iv.soc_wh for iv in ivs]
    span_h = (ivs[-1].start - ivs[0].start).total_seconds() / 3600

    return {
        "interval_minutes": interval_minutes(ivs),
        "first_interval": iso_z(ivs[0].start),
        "horizon_hours": round(span_h, 2),
        "actions": dict(counts),
        "self_starts": self_starts,
        "total_charge_wh": round(sum(i.charge_wh for i in ivs)),
        "total_discharge_wh": round(sum(i.discharge_wh for i in ivs)),
        "min_soc_pct": round(100 * min(socs) / capacity_wh, 1),
        "max_soc_pct": round(100 * max(socs) / capacity_wh, 1),
        "soc_span_pct": round(100 * (max(socs) - min(socs)) / capacity_wh, 1),
        "negative_buy_intervals": sum(1 for i in ivs if i.price_buy < 0),
        "negative_sell_intervals": sum(1 for i in ivs if i.price_sell < 0),
        # The oldest runs tag themselves `...+02:00` and newer ones `...Z`. Recorded because
        # it is the exact trap `newest_by_interval()` parses rather than string-sorts for, and
        # a corpus with only one format would not exercise it.
        "plan_run_format": "offset" if "+" in intervals[0].plan_run else "utc",
    }


def select(runs: dict[str, dict], budget: int) -> list[tuple[str, str]]:
    """Pick the runs a human should review. Returns [(plan_run, why)], in priority order.

    Deterministic: same archive in, same corpus out, so re-fetching does not silently churn
    which runs the goldens cover.
    """
    picked: list[tuple[str, str]] = []
    chosen: set[str] = set()

    def take(tag: str | None, why: str) -> None:
        if tag and tag not in chosen and len(picked) < budget:
            chosen.add(tag)
            picked.append((tag, why))

    def best(key):
        """The unchosen run scoring highest on `key`. Negate the key to want the lowest."""
        return max((t for t in runs if t not in chosen), key=key, default=None)

    # 1. `self` coverage first, and greedily, because it is the scarcest shape in the archive
    #    -- a few dozen intervals out of ~12,500. It is also the action whose correctness is
    #    least obvious (section 4.1: the right command is NO command), so it is the one most
    #    worth having a human look at.
    covered: set[str] = set()
    for _ in range(3):
        cand = max(
            (t for t in runs if t not in chosen),
            key=lambda t: len(set(runs[t]["self_starts"]) - covered),
            default=None)
        if cand is None or not (set(runs[cand]["self_starts"]) - covered):
            break
        new = len(set(runs[cand]["self_starts"]) - covered)
        covered |= set(runs[cand]["self_starts"])
        take(cand, f"adds {new} previously-uncovered `self` intervals")

    # 2. Overlapping horizons: the run immediately following the first pick. Section 3.3's
    #    newest-wins rule only means anything with two runs covering the same instants, so the
    #    corpus has to contain a consecutive pair BY CONSTRUCTION. This sits above the
    #    extremes below because it is a structural requirement of the test suite, not a
    #    stress case -- ranked lower, it gets squeezed out by the budget, which is exactly
    #    what happened on the first survey run.
    #    Compared as parsed instants, like the newest-run pick below: a `>` between two tag
    #    STRINGS puts a `+02:00` run after a `Z` run from a later instant, so "the run
    #    immediately following" could be a run that precedes it.
    if picked:
        after = sorted((t for t in runs if run_time(t) > run_time(picked[0][0])), key=run_time)
        take(after[0] if after else None, f"consecutive with {picked[0][0]} -- overlapping horizons")

    # 3. The NEWEST run. Structural, not a stress case: it is the only run whose horizon
    #    overlaps what the battery is doing right now, so it is the one a reviewer can check
    #    against the live dashboard instead of against their own arithmetic. Selection is
    #    otherwise entirely by shape, and shape is a property of the past -- on the
    #    2026-08-15T18:19:44Z fetch that left all seven of that day's runs unselected, and
    #    the newest run on the page three days stale, on a page whose whole purpose is to be
    #    read against today.
    #    Parsed, never string-sorted: the archive mixes `...Z` and `...+02:00` tags, and
    #    lexical order puts a `+02:00` run before a `Z` run from the same instant. That is
    #    the same trap `newest_by_interval()` parses for.
    take(max(runs, key=run_time) if runs else None, "newest run in the archive")

    # 4. The oldest run, for the `+02:00` plan_run tag. Also structural: a format the overlap
    #    resolver must parse rather than string-sort, and only the archive's tail has it.
    offset = [t for t in runs if runs[t]["plan_run_format"] == "offset"]
    take(min(offset, key=run_time) if offset else None, "oldest run -- `+02:00` plan_run tag")

    # 5. The extremes, with whatever budget is left. A golden over an average day proves the
    #    code runs; a golden over the deepest cycle in the archive proves it runs where the
    #    arithmetic is under strain.
    take(best(lambda t: runs[t]["total_discharge_wh"]), "deepest total discharge")
    take(best(lambda t: runs[t]["total_charge_wh"]), "deepest total charge")
    take(best(lambda t: -runs[t]["min_soc_pct"]), "lowest SoC -- reserve-binding tail")
    take(best(lambda t: runs[t]["soc_span_pct"]), "widest SoC swing in one run")

    return picked


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default=os.environ.get("INFLUX_URL", "http://192.168.68.105:8086"))
    p.add_argument("--org", default=os.environ.get("INFLUX_ORG", "home"))
    p.add_argument("--bucket", default="planning")
    p.add_argument("--token-env", default="INFLUX_TOKEN_PLANNING")
    p.add_argument("--start", default="2026-07-01T00:00:00Z")
    p.add_argument("--stop", default=None, help="default: 48 h from now, to include horizons")
    p.add_argument("--capacity-wh", type=float, default=DEFAULT_CAPACITY_WH)
    p.add_argument("--floor-wh", type=float, default=ENERGY_FLOOR_WH)
    p.add_argument("--review-budget", type=int, default=9)
    p.add_argument("--survey", action="store_true", help="Print the archive, write nothing")
    p.add_argument("--selected-only", action="store_true",
                   help="Write only the reviewed runs, not the whole archive")
    a = p.parse_args()

    out = corpus.TESTDATA_DIR
    if not a.survey:
        assert_gitignored(out)

    from influxdb_client import InfluxDBClient

    start = dt.datetime.fromisoformat(a.start.replace("Z", "+00:00"))
    stop = (dt.datetime.fromisoformat(a.stop.replace("Z", "+00:00")) if a.stop
            else dt.datetime.now(dt.UTC) + dt.timedelta(hours=48))

    print(f"querying {a.url} bucket={a.bucket} {start:%Y-%m-%d} -> {stop:%Y-%m-%d}")
    with InfluxDBClient(url=a.url, token=env(a.token_env), org=a.org) as client:
        intervals = from_influx(client.query_api(), a.bucket, start, stop)
    print(f"  {len(intervals)} points")

    by_run: dict[str, list] = defaultdict(list)
    for iv in intervals:
        by_run[iv.plan_run].append(iv)
    if "" in by_run:
        # Untagged points cannot be attributed to a run, and the overlap rule is defined in
        # terms of runs. Loud, because it would mean the planner's write path changed.
        print(f"  WARNING: {len(by_run.pop(''))} points carry no plan_run tag -- skipped")

    runs: dict[str, dict] = {}
    unreadable: list[str] = []
    for tag, ivs in by_run.items():
        try:
            runs[tag] = features_of(ivs, a.capacity_wh, a.floor_wh)
        except (PlanFormatError, ValueError) as e:
            # A single malformed run must not cost the whole corpus. Reported at the end.
            unreadable.append(f"{tag}: {e}")
    print(f"  {len(runs)} plan runs ({len(unreadable)} unreadable)")

    picks = select(runs, a.review_budget)
    why = dict(picks)

    totals: dict[str, int] = defaultdict(int)
    for f in runs.values():
        for k, v in f["actions"].items():
            totals[k] += v
    grand = sum(totals.values())
    print("\naction mix across the whole archive:")
    for k in sorted(totals, key=lambda k: -totals[k]):
        print(f"  {k:<10} {totals[k]:>6}  {100 * totals[k] / grand:5.2f} %")
    if not totals.get("self"):
        print("  NOTE: no `self` intervals anywhere -- section 4.1's most subtle branch is "
              "untested by real data and needs the synthetic fixture")

    print(f"\nselected for review ({len(picks)} of {len(runs)}):")
    for tag, reason in picks:
        f = runs[tag]
        print(f"  {tag}  {f['interval_minutes']:>2}min {f['horizon_hours']:>5.1f}h  "
              f"soc {f['min_soc_pct']:>4.1f}-{f['max_soc_pct']:<5.1f}%  "
              f"{dict(f['actions'])}\n      {reason}")

    if a.survey:
        print("\n--survey: nothing written")
        return 0

    written = 0
    for tag, ivs in by_run.items():
        if tag not in runs:
            continue
        if a.selected_only and tag not in why:
            continue
        f = dict(runs[tag])
        f["selected_for_review"] = tag in why
        f["selection_reason"] = why.get(tag, "")
        corpus.dump_run(ivs, f, out / corpus.run_filename(tag))
        written += 1

    manifest = {
        "fetched_at": iso_z(dt.datetime.now(dt.UTC)),
        "source": {"url": a.url, "bucket": a.bucket, "org": a.org},
        "window": {"start": a.start, "stop": iso_z(stop)},
        "capacity_wh": a.capacity_wh,
        "energy_floor_wh": a.floor_wh,
        "run_count": written,
        "point_count": len(intervals),
        "action_totals": dict(totals),
        "selected": [{"plan_run": t, "reason": r} for t, r in picks],
        "unreadable": unreadable,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"\nwrote {written} runs + manifest.json to {out.relative_to(REPO)}/")
    if unreadable:
        print("unreadable runs:")
        for u in unreadable:
            print(f"  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
