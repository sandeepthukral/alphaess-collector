"""Did the dispatch loop run, and did what it decided reach the battery?

Pure analysis: no I/O, no HTML, no Influx client. It takes tick rows and collector series
that somebody else fetched, and returns structured findings that somebody else renders.

WHY IT IS A MODULE. `scripts/review-dry-run.py` grew this logic and owns the one HTML page
that gates going live; `scripts/is-it-deciding.py` needs the same constants for the same
judgement about the same loop, and declared its own copies bound to the originals by a
comment. `review_page.py`'s docstring already argues against exactly that. Section 5.4's
nightly `dispatch_health` job is the third caller, and three copies of "how long is too
long" is how a monitor and the page it links to end up disagreeing about whether last night
was fine.

THE SPLIT THAT MATTERS. `findings()` used to build HTML strings, so it could only ever
serve the page. Analysis now returns `Finding` records carrying the numbers; the page
formats them into `<li>`s and the rollup counts them. Neither can drift from the other's
idea of what a fault is, because there is only one.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from plan import run_time

# The dispatcher's own loop interval. A gap materially longer than this means the loop missed
# a tick, which in dry run is otherwise invisible: `action` reads `no dispatch` either way.
TICK_S = 60
# Two missed ticks. One slow tick is a slow Modbus read, which is normal and not worth a
# finding; three minutes of silence is the loop not running.
GAP_S = 180
# A hole in the COLLECTOR's own sampling, near a dispatch gap, that means both processes
# stopped together rather than the dispatch loop dying on its own. The collector samples about
# every 30 s and the callers average into one-minute buckets, so consecutive points are
# normally 60 s apart; 150 s is at least two missing buckets and cannot be jitter.
COLLECTOR_GAP_S = 150
# How far either side of the dispatch gap to look for that hole. THE TWO DO NOT ALIGN, and
# expecting them to is the mistake this constant exists to fix: they poll at different rates,
# over different transports, with different timeouts, so a network spell takes them out at
# different moments and releases them at different moments. Measured strictly inside the
# dispatch gap, the real 02:14 stall of 2026-08-18 scored 123 s -- under the threshold -- only
# because the collector held on 110 s longer at the start and recovered 43 s later at the end.
# Two minutes covers that skew without reaching into unrelated minutes.
MATCH_PAD_S = 120
# A plan older than this is one the translator should already have replaced -- it runs every
# five minutes, against a planner that runs hourly. Tracks `slots.MAX_PLAN_AGE`, which is two
# missed planner runs; see monitor #4 in DESIGN-dispatch.md section 6.1.
STALE_PLAN_S = 2 * 3600
# Below this, `slots.decide()` refuses a discharge (the direction guard). A discharge decided
# here would be a translator bug, and is the one decision on this page that is a real fault.
DEFAULT_SOC_FLOOR = 10.0
# Battery power below this is noise, not a direction. The same floor the corpus review uses
# for classifying an interval as doing something.
IDLE_W = 50

# The `dispatch_state` fields the analysis reads, for callers to hand to their pivot query.
#
# `read_error` is here for a reason that is easy to miss: WITHOUT IT, A DEGRADED TICK IS NOT
# A ROW. A tick that decided but could not read the inverter publishes `read_error` and the
# decision, and none of the other five fields when no slot is active -- so it contributed
# nothing to the pivot and vanished. Not as a fault, not as a stall: as a GAP, because the
# rows either side of it were minutes apart. The page reported an unreachable inverter as a
# dead loop, which is the opposite diagnosis.
FIELDS = ("slot_action", "action", "plan_run", "setpoint_w", "dispatch_active", "read_error")

# What a decision means when the tick recorded none. `self` is section 4.1's deliberate
# RELEASE of dispatch, not the absence of a decision.
NO_SLOT = "self"


@dataclass(frozen=True)
class Finding:
    """One thing worth saying about the window, with the numbers that justify it.

    `severity` decides which block a renderer puts it in, and the three are read completely
    differently:

      fault    -- something in `dispatch/` to fix. The list that gates going live.
      preview  -- a behaviour change going live would cause. The reason for doing it at all.
      stall    -- the tick stream stopped because something upstream of the whole stack
                  stopped. Neither of the above: no change to `dispatch/` would prevent it.
      degraded -- the loop alive and deciding, with the inverter unreadable. Like a stall in
                  that nothing here fixes it, unlike a stall in that the loop kept its head:
                  it is the fail-safe being exercised, and it is evidence rather than damage.

    Mixing stalls into faults, which this page did until 2026-08-18, makes a bad-network
    night look like a broken dispatcher. Mixing previews in makes a normal dry-run day look
    alarming.
    """

    kind: str
    severity: str
    detail: dict = field(default_factory=dict)


SEVERITIES = ("fault", "preview", "stall", "degraded")


def decision_runs(ticks: list[dict]) -> list[dict]:
    """Collapse per-minute ticks into contiguous runs of the same decision.

    A day is ~1,440 ticks and perhaps 20 decisions. The runs are what a human reviews; the
    ticks are what the gap check needs. Both are kept.
    """
    runs: list[dict] = []
    for t in ticks:
        act = t.get("slot_action") or NO_SLOT
        if runs and runs[-1]["action"] == act and \
                (t["_time"] - runs[-1]["last"]).total_seconds() <= GAP_S:
            runs[-1]["last"] = t["_time"]
            runs[-1]["ticks"] += 1
        else:
            runs.append({"action": act, "start": t["_time"], "last": t["_time"], "ticks": 1,
                         "plan_run": t.get("plan_run", "")})
    for r in runs:
        # The run owns the minute it last decided in, so the band reaches the next decision
        # rather than stopping a tick short and leaving a hairline gap between every band.
        r["end"] = r["last"] + dt.timedelta(seconds=TICK_S)
    return runs


def mean_between(series, t0, t1):
    vals = [v for t, v in series if t0 <= t < t1]
    return sum(vals) / len(vals) if vals else None


def find_gaps(ticks: list[dict]) -> list[tuple[dt.datetime, dt.datetime, float]]:
    out = []
    for a, b in zip(ticks, ticks[1:]):
        gap = (b["_time"] - a["_time"]).total_seconds()
        if gap > GAP_S:
            out.append((a["_time"], b["_time"], gap))
    return out


def longest_hole(series, t0: dt.datetime, t1: dt.datetime) -> float:
    """Seconds of the longest stretch inside [t0, t1] where `series` has no point.

    The window edges count as boundaries, so a series that simply stops before `t0` and
    resumes after `t1` reports the whole window rather than zero -- which is the answer that
    matters, since that is exactly the shape of a collector that was also down.
    """
    edges = [t0] + [t for t, _ in series if t0 < t < t1] + [t1]
    return max((b - a).total_seconds() for a, b in zip(edges, edges[1:]))


def attribute_gap(battery, t0: dt.datetime, t1: dt.datetime) -> tuple[str, float]:
    """(cause, longest collector hole) for one dispatch gap.

    WHY THE COLLECTOR IS THE CONTROL. This page used to assert that a gap was "the loop not
    running", which it cannot know: the two processes reach entirely different things --
    dispatch talks Modbus to the inverter over the LAN, the collector talks HTTPS to the
    AlphaESS cloud over the WAN -- so nothing short of something upstream of both stalls
    both. On 2026-08-17/18 all six gaps were of that kind, during a spell of bad home
    networking, and the page called every one of them a dispatch fault. Stating a cause with
    no evidence, on the most alarming item on the page, is the worst place to be confidently
    wrong.

    `unknown` when there is no collector data at all in the window: with no control series
    the two causes are indistinguishable, and saying so beats picking one.
    """
    if not battery:
        return "unknown", 0.0
    pad = dt.timedelta(seconds=MATCH_PAD_S)
    hole = longest_hole(battery, t0 - pad, t1 + pad)
    return ("network" if hole >= COLLECTOR_GAP_S else "dispatch"), hole


def armed_ticks(ticks: list[dict]) -> list[dict]:
    """Ticks that found the dispatch block armed by something that is not this dispatcher.

    The predicate is `exists action and action != "no dispatch"`, and the `exists` half is
    load-bearing. A degraded tick deliberately publishes NO `action` at all -- `state.py`
    is explicit that the honest report of an unreadable inverter is a missing field, not a
    stale one. Read as `(t.get("action") or "") != "no dispatch"`, a missing field became
    `""`, `""` is not `"no dispatch"`, and an ordinary Modbus timeout was reported as the
    most alarming finding the page has: the block ARMED by a foreign controller, with the
    reason rendering the literal string `"None"`.
    """
    return [t for t in ticks if (t.get("action") or None) not in (None, "no dispatch")]


def degraded_ticks(ticks: list[dict]) -> list[dict]:
    return [t for t in ticks if t.get("read_error")]


def plan_age_s(tick: dict) -> float | None:
    """Seconds between a tick and the plan it acted on, or None if it named no plan.

    Via `plan.run_time`, which is the one place the tag-parsing rule lives: tags written
    before 2026-07-30 carry `+02:00` where later ones carry `Z`, so these are never
    compared as strings.
    """
    tag = tick.get("plan_run")
    if not tag:
        return None
    return (tick["_time"] - run_time(str(tag))).total_seconds()


def analyse(ticks, runs, battery, soc, soc_floor=DEFAULT_SOC_FLOOR) -> list[Finding]:
    """Every finding for the window, in the order a reader wants them.

    Returns records, not sentences. `scripts/review-dry-run.py` turns them into the page;
    the nightly rollup counts them into `dispatch_health`. That both read the same list is
    the point: a monitor that says last night was fine, linking to a page that says it was
    not, is worse than having neither.
    """
    out: list[Finding] = []

    for t0, t1, gap in find_gaps(ticks):
        cause, hole = attribute_gap(battery, t0, t1)
        out.append(Finding(
            kind="gap",
            severity="stall" if cause == "network" else "fault",
            detail={"start": t0, "end": t1, "gap_s": gap, "hole_s": hole, "cause": cause}))

    armed = armed_ticks(ticks)
    if armed:
        out.append(Finding(
            kind="armed", severity="fault",
            detail={"ticks": len(armed),
                    "kinds": sorted({str(t.get("action")) for t in armed})}))

    for r in runs:
        if r["action"] != "discharge":
            continue
        window = [v for t, v in soc if r["start"] <= t < r["end"]]
        lo = min(window) if window else None
        if lo is not None and lo < soc_floor:
            out.append(Finding(
                kind="discharge_below_floor", severity="fault",
                detail={"start": r["start"], "end": r["end"], "soc_pct": lo,
                        "floor_pct": soc_floor}))

    ages = [a for t in ticks if (a := plan_age_s(t)) is not None and a > STALE_PLAN_S]
    if ages:
        out.append(Finding(
            kind="stale_plan", severity="fault",
            detail={"ticks": len(ages), "worst_s": max(ages)}))

    blind = degraded_ticks(ticks)
    if blind:
        out.append(Finding(
            kind="blind", severity="degraded",
            detail={"ticks": len(blind),
                    "errors": sorted({str(t["read_error"]) for t in blind})[:5],
                    "distinct": len({str(t["read_error"]) for t in blind})}))

    for r in runs:
        # `self` is excluded on purpose and is not an oversight: section 4.1 makes it the
        # deliberate RELEASE of dispatch, so the battery running self-consumption under it is
        # the decision being honoured. There is nothing going live would change.
        if r["action"] == NO_SLOT:
            continue
        actual = mean_between(battery, r["start"], r["end"])
        if actual is None:
            continue
        # `battery_power_w` is the collector's raw sign convention: positive means the battery
        # is DISCHARGING. Flipped once here so both sides of the comparison are
        # charging-positive, matching `setpoint_w` and every panel on the dashboard.
        actual_cp = -actual

        if r["action"] == "hold":
            # A hold freezes the battery at 0 W. Against a battery that was moving, that is
            # the largest behaviour change on this page and the easiest to overlook, because
            # "hold" sounds like "do nothing" -- it is not, it is Mode 3 actively holding the
            # battery still while the house runs off the grid.
            diverged = abs(actual_cp) >= IDLE_W
            wanted = "frozen at 0 W"
        else:
            wanted_sign = 1 if r["action"] == "charge" else -1
            diverged = (abs(actual_cp) < IDLE_W
                        or (actual_cp > 0) != (wanted_sign > 0))
            wanted = "charging" if wanted_sign > 0 else "discharging"

        if diverged:
            out.append(Finding(
                kind="divergence", severity="preview",
                detail={"start": r["start"], "end": r["end"], "action": r["action"],
                        "wanted": wanted, "actual_w": actual_cp, "ticks": r["ticks"]}))

    return out


def by_severity(found: list[Finding]) -> dict[str, list[Finding]]:
    """Findings grouped, with every severity present even when empty.

    Always all four keys, so a renderer's "nothing to report" branch is chosen by an empty
    list rather than by a missing key -- a `KeyError` on the good day is the one failure
    mode nobody exercises.
    """
    out: dict[str, list[Finding]] = {s: [] for s in SEVERITIES}
    for f in found:
        out[f.severity].append(f)
    return out
