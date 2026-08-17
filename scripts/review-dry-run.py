#!/usr/bin/env python3
"""What the dispatcher decided while in dry run, against what the battery actually did.

    scripts/review-dry-run.py                      # since local midnight
    scripts/review-dry-run.py --hours 36
    scripts/review-dry-run.py --start 2026-08-17T18:00:00Z

Writes one self-contained `dispatch/testdata/review-dry-run.html` -- inside the gitignored
directory by default, because a day of this household's SoC and power curve is not something
to leave sitting in a public repo. DISPATCH-GOLIVE.md section 3, box four.

WHY THIS EXISTS. That box says "watch a full day of dry-run decisions against what the
battery actually did", and a day of glancing at a stat panel is not a review -- it samples
whatever moments you happened to look, and the interesting minute is the one you missed.
The dispatcher publishes a decision every 60 s; this reads all of them at once.

WHAT DRY RUN MAKES HARD, AND WHAT THIS PAGE DOES ABOUT IT.

`setpoint_w` IS NOT THE INTENDED COMMAND HERE. `scheduler.py:342` publishes a READBACK of the
register block, and in dry run nothing is ever written, so `setpoint_w` is 0 and `action` is
`no dispatch` on every tick of the day no matter what was decided. Reading either as "what
dispatch wanted" would be reading the inverter's resting state and calling it a plan. The
decision is carried by `slot_action` and `plan_run`, which `state.py:106` publishes from the
slot rather than from the registers, precisely so this distinction survives. So this page is
built on `slot_action`, and says so on its face.

DIVERGENCE IS THE POINT, NOT THE FAULT. Because nothing is commanded, the battery spends the
whole day on self-consumption, and it will disagree with the decisions constantly. That
disagreement is not an error to be counted -- it is exactly the behaviour change that going
live would buy, which makes it the most useful thing on the page and the easiest thing to
misread. Every divergence here is labelled as a PREVIEW. The genuine faults are separate and
few: gaps in the tick stream, a block armed by something that is not this dispatcher, a plan
gone stale, a discharge decided under the SoC floor.

Reads InfluxDB only, and only the `alphaess` bucket. Self-contained SVG, no libraries: it is
published as an artifact and read on a phone.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_page  # noqa: E402
from review_page import COLOURS, TZ_LABEL, hour_step, local  # noqa: E402

# The dispatcher's own loop interval. A gap materially longer than this means the loop missed
# a tick, which in dry run is otherwise invisible: `action` reads `no dispatch` either way.
TICK_S = 60
# Two missed ticks. One slow tick is a slow Modbus read, which is normal and not worth a
# finding; three minutes of silence is the loop not running.
GAP_S = 180
# A plan older than this is one the translator should already have replaced -- it runs every
# three hours. Matches monitor #4's window in DESIGN-dispatch.md section 6.1.
STALE_PLAN_S = 4 * 3600
# Below this, `slots.decide()` refuses a discharge (the direction guard). A discharge decided
# here would be a translator bug, and is the one decision on this page that is a real fault.
DEFAULT_SOC_FLOOR = 10.0
# Battery power below this is noise, not a direction. The same floor the corpus review uses
# for classifying an interval as doing something.
IDLE_W = 50

W, H_MAIN, H_PRICE, PAD_L, PAD_R, PAD_T = 1120, 250, 90, 52, 20, 14

DECISION_HELP = {
    "charge": "the plan wanted a forced charge here",
    "discharge": "the plan wanted a forced discharge here",
    "self": "the plan wanted dispatch RELEASED -- plain self-consumption",
    "hold": "the plan wanted the battery frozen at 0 W",
}


def assert_gitignored(path: Path) -> None:
    """Refuse to write a household's SoC and power curve into a public repo.

    Same guard, and the same reasoning, as `fetch-plan-corpus.py:59` -- checked rather than
    trusted, because the entry protecting that directory has been deleted once already. This
    page is less sensitive than the plan archive (no `load_forecast_wh`, which is occupancy at
    15-minute resolution) but it is still a full day of one house's battery behaviour, and the
    default output lands inside the repo.
    """
    r = subprocess.run(["git", "check-ignore", "-v", str(path)],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"REFUSING TO WRITE: {path} is not gitignored, and this repo is public.\n"
                 f"Write it under dispatch/testdata/, or add it to .gitignore.")


def q_pivot(api, bucket, measurement, fields, start, stop):
    """Rows of {_time: ..., field: value} for one measurement, newest schema wins.

    Pivoted in Flux rather than stitched in Python because a tick's fields share one
    timestamp by construction, and that is the only thing that makes them one decision.
    """
    sel = " or ".join(f'r._field == "{f}"' for f in fields)
    tables = api.query(f'''from(bucket: "{bucket}")
  |> range(start: {start.isoformat().replace("+00:00", "Z")}, '''
                      f'''stop: {stop.isoformat().replace("+00:00", "Z")})
  |> filter(fn: (r) => r._measurement == "{measurement}" and ({sel}))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> sort(columns: ["_time"])''')
    out = []
    for t in tables:
        for rec in t.records:
            row = {k: v for k, v in rec.values.items() if not k.startswith("_") or k == "_time"}
            row["_time"] = rec.get_time()
            out.append(row)
    return out


def q_series(api, bucket, measurement, field, start, stop, every="1m"):
    """One numeric series, averaged into `every` buckets.

    Averaged rather than sampled: `battery_power_w` arrives far faster than the dispatcher
    decides, and drawing every raw point would put 8,000 nodes in an SVG meant for a phone
    while telling the reader nothing a one-minute mean does not.
    """
    tables = api.query(f'''from(bucket: "{bucket}")
  |> range(start: {start.isoformat().replace("+00:00", "Z")}, '''
                      f'''stop: {stop.isoformat().replace("+00:00", "Z")})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
  |> group()
  |> sort(columns: ["_time"])''')
    return [(rec.get_time(), rec.get_value())
            for t in tables for rec in t.records if rec.get_value() is not None]


def decision_runs(ticks: list[dict]) -> list[dict]:
    """Collapse per-minute ticks into contiguous runs of the same decision.

    A day is ~1,440 ticks and perhaps 20 decisions. The runs are what a human reviews; the
    ticks are what the gap check needs. Both are kept.
    """
    runs: list[dict] = []
    for t in ticks:
        act = t.get("slot_action") or "self"
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


def findings(ticks, runs, battery, soc, soc_floor) -> tuple[list[str], list[str]]:
    """(faults, previews). Two lists, because they are read completely differently.

    A fault is something to fix before going live. A preview is a behaviour change going live
    would cause -- the reason for doing it at all. Mixing them into one "warnings" list, as
    the corpus review can afford to, would make a normal dry-run day look alarming.
    """
    faults, previews = [], []

    for t0, t1, gap in find_gaps(ticks):
        faults.append(
            f"<b>{gap / 60:.0f} min with no decision</b>, "
            f"{local(t0):%H:%M} &rarr; {local(t1):%H:%M}. The loop ticks every {TICK_S} s; "
            f"a silence this long is the loop not running, and in dry run nothing else shows "
            f"it &mdash; <code>action</code> reads <code>no dispatch</code> either way")

    armed = [t for t in ticks if (t.get("action") or "") != "no dispatch"]
    if armed:
        kinds = ", ".join(sorted({str(t.get("action")) for t in armed}))
        faults.append(
            f"<b>{len(armed)} tick(s) found the dispatch block ARMED</b> ({kinds}). Dry run "
            f"writes nothing, so this dispatcher did not do it: something else is driving the "
            f"registers &mdash; the app&rsquo;s price control, or a leftover command. This is "
            f"monitor #7&rsquo;s case and it blocks going live")

    for r in runs:
        if r["action"] != "discharge":
            continue
        window = [v for t, v in soc if r["start"] <= t < r["end"]]
        lo = min(window) if window else None
        if lo is not None and lo < soc_floor:
            faults.append(
                f"<b>discharge decided at {lo:.1f}% SoC</b>, below the {soc_floor:.0f}% floor, "
                f"{local(r['start']):%H:%M}&ndash;{local(r['end']):%H:%M}. "
                f"<code>slots.decide()</code>&rsquo;s direction guard should have refused this")

    stale = [t for t in ticks if t.get("plan_run") and
             (t["_time"] - review_page.parse_ts(str(t["plan_run"]))).total_seconds()
             > STALE_PLAN_S]
    if stale:
        worst = max((t["_time"] - review_page.parse_ts(str(t["plan_run"]))).total_seconds()
                    for t in stale)
        faults.append(
            f"<b>{len(stale)} tick(s) acted on a plan over "
            f"{STALE_PLAN_S / 3600:.0f} h old</b>, worst {worst / 3600:.1f} h. The translator "
            f"runs every 3 h, so this means it stopped &mdash; monitor #3")

    for r in runs:
        # `self` is excluded on purpose and is not an oversight: section 4.1 makes it the
        # deliberate RELEASE of dispatch, so the battery running self-consumption under it is
        # the decision being honoured. There is nothing going live would change.
        if r["action"] == "self":
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
            previews.append(
                f"{local(r['start']):%H:%M}&ndash;{local(r['end']):%H:%M} decided "
                f"<b>{r['action']}</b> ({wanted}); the battery was actually doing "
                f"{actual_cp:+.0f} W. Going live would have overridden self-consumption "
                f"for {r['ticks']} min here")

    return faults, previews


def svg_chart(runs, battery, soc, price, t0, t1) -> str:
    total = (t1 - t0).total_seconds()
    plot_w = W - PAD_L - PAD_R

    def x(t):
        return PAD_L + plot_w * (t - t0).total_seconds() / total

    y_main_0, y_main_1 = PAD_T, PAD_T + H_MAIN

    def y_soc(pct):
        return y_main_1 - H_MAIN * pct / 100.0

    pmax = max([*(abs(v) for _, v in battery), 1000])

    def y_pow(w):
        return (y_main_0 + y_main_1) / 2 - (H_MAIN / 2) * (w / pmax)

    y_pr_1 = y_main_1 + 38 + H_PRICE
    prices = [v for _, v in price]
    pr_hi, pr_lo = max([*prices, 0.01]), min([*prices, 0.0])
    pr_rng = (pr_hi - pr_lo) or 1.0

    def y_price(p):
        return y_pr_1 - H_PRICE * (p - pr_lo) / pr_rng

    o = [f'<svg viewBox="0 0 {W} {y_pr_1 + 48}" class="chart" '
         f'xmlns="http://www.w3.org/2000/svg">']

    for r in runs:
        x0, x1 = x(max(r["start"], t0)), x(min(r["end"], t1))
        if x1 <= x0:
            continue
        o.append(f'<rect x="{x0:.1f}" y="{y_main_0}" width="{max(x1 - x0, 0.6):.1f}" '
                 f'height="{H_MAIN}" fill="{COLOURS[r["action"]]}" opacity="0.16"/>')

    for pct in (0, 25, 50, 75, 100):
        yy = y_soc(pct)
        o.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" '
                 f'stroke="var(--grid)" stroke-width="1"/>')
        o.append(f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" class="ax" '
                 f'text-anchor="end">{pct}%</text>')
    ymid = (y_main_0 + y_main_1) / 2
    o.append(f'<line x1="{PAD_L}" y1="{ymid:.1f}" x2="{W - PAD_R}" y2="{ymid:.1f}" '
             f'stroke="var(--ink)" stroke-width="1" stroke-dasharray="2 3" opacity="0.45"/>')

    px_per_hour = plot_w / (total / 3600.0)
    step, half_ticks = hour_step(px_per_hour)
    first = local(t0).replace(minute=0, second=0, microsecond=0)
    if first < local(t0):
        first += dt.timedelta(hours=1)
    t = first
    while t.astimezone(dt.UTC) <= t1:
        utc = t.astimezone(dt.UTC)
        xx, midnight = x(utc), t.hour == 0
        if midnight or t.hour % step == 0:
            o.append(f'<line x1="{xx:.1f}" y1="{y_main_0}" x2="{xx:.1f}" y2="{y_pr_1}" '
                     f'stroke="var(--ink)" stroke-width="1" '
                     f'opacity="{0.28 if midnight else 0.10}"/>')
            o.append(f'<text x="{xx:.1f}" y="{y_pr_1 + 18}" class="ax" '
                     f'text-anchor="middle">{t:%H}</text>')
            if midnight:
                o.append(f'<text x="{xx + 4:.1f}" y="{y_pr_1 + 34}" class="ax day">'
                         f'{t:%a %d %b}</text>')
        elif half_ticks:
            o.append(f'<line x1="{xx:.1f}" y1="{y_pr_1}" x2="{xx:.1f}" y2="{y_pr_1 + 3}" '
                     f'stroke="var(--ink)" stroke-width="1" opacity="0.3"/>')
        t += dt.timedelta(hours=1)

    # Price bars, drawn under the main panel on their own scale. Each bar runs from its own
    # timestamp to the next one, and the last runs to the end of the window -- a price is a
    # value over an interval, and drawing it as a point would leave the final hour blank.
    edges = [t for t, _ in price] + [t1]
    for (pt, pv), nxt in zip(price, edges[1:]):
        x0, x1 = x(pt), x(min(nxt, t1))
        yy = y_price(pv)
        o.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{max(x1 - x0 - 1, 0.8):.1f}" '
                 f'height="{max(y_pr_1 - yy, 0.5):.1f}" '
                 f'fill="{"#dc2626" if pv < 0 else "var(--price)"}" opacity="0.55"/>')

    # Actual battery power, charging-positive to match the dashboard and `setpoint_w`.
    if battery:
        pts = " ".join(f"{x(t):.1f},{y_pow(-v):.1f}" for t, v in battery)
        o.append(f'<polyline points="{pts}" fill="none" stroke="var(--pow)" '
                 f'stroke-width="1.4" opacity="0.9"/>')

    # Actual SoC.
    if soc:
        pts = " ".join(f"{x(t):.1f},{y_soc(v):.1f}" for t, v in soc)
        o.append(f'<polyline points="{pts}" fill="none" stroke="var(--soc)" '
                 f'stroke-width="2.2"/>')

    o.append("</svg>")
    return "".join(o)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default=os.environ.get("INFLUX_URL", "http://192.168.68.105:8086"))
    p.add_argument("--org", default=os.environ.get("INFLUX_ORG", "home"))
    p.add_argument("--bucket", default="alphaess")
    p.add_argument("--token-env", default="INFLUX_TOKEN_GRAFANA",
                   help="env var holding the token; read-only is enough")
    p.add_argument("--start", default=None, help="ISO instant; default local midnight")
    p.add_argument("--hours", type=float, default=None, help="window ending now")
    p.add_argument("--soc-floor", type=float,
                   default=float(os.environ.get("SOC_FLOOR_PCT", DEFAULT_SOC_FLOOR)))
    # Defaults inside `dispatch/testdata/`, which is already gitignored, so the safe path is
    # the one you get by not thinking about it.
    p.add_argument("--out", default=str(REPO / "dispatch/testdata/review-dry-run.html"))
    a = p.parse_args()

    out = Path(a.out).resolve()
    if REPO in out.parents:
        assert_gitignored(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    token = os.environ.get(a.token_env)
    if not token:
        sys.exit(f"{a.token_env} is not set -- export it or pass --token-env")

    now = dt.datetime.now(dt.UTC)
    if a.start:
        t0 = review_page.parse_ts(a.start)
    elif a.hours:
        t0 = now - dt.timedelta(hours=a.hours)
    else:
        t0 = local(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.UTC)
    t1 = now

    from influxdb_client import InfluxDBClient

    print(f"querying {a.url} bucket={a.bucket} {local(t0):%Y-%m-%d %H:%M} -> "
          f"{local(t1):%H:%M} {TZ_LABEL}")
    with InfluxDBClient(url=a.url, token=token, org=a.org) as client:
        api = client.query_api()
        ticks = q_pivot(api, a.bucket, "dispatch_state",
                        ["slot_action", "action", "plan_run", "setpoint_w", "dispatch_active"],
                        t0, t1)
        battery = q_series(api, a.bucket, "power_readings", "battery_power_w", t0, t1)
        soc = q_series(api, a.bucket, "power_readings", "soc_percent", t0, t1)
        price = q_series(api, a.bucket, "market_price", "total", t0, t1, every="1h")

    if not ticks:
        sys.exit("no dispatch_state points in that window -- is the dispatcher running?")

    runs = decision_runs(ticks)
    faults, previews = findings(ticks, runs, battery, soc, a.soc_floor)
    counts = Counter(r["action"] for r in runs)
    tick_counts = Counter((t.get("slot_action") or "self") for t in ticks)
    plan_runs = sorted({str(t["plan_run"]) for t in ticks if t.get("plan_run")})

    rows = []
    for r in runs:
        act = mean_between(battery, r["start"], r["end"])
        s0 = mean_between(soc, r["start"], r["start"] + dt.timedelta(seconds=TICK_S))
        rows.append(
            f"<tr><td>{local(r['start']):%H:%M}</td><td>{local(r['end']):%H:%M}</td>"
            f"<td><span class='dot' style='background:{COLOURS[r['action']]}'></span>"
            f"{r['action']}</td><td class='n'>{r['ticks']}</td>"
            f"<td class='n'>{'' if act is None else f'{-act:+.0f} W'}</td>"
            f"<td class='n'>{'' if s0 is None else f'{s0:.1f}%'}</td>"
            f"<td>{html.escape(str(r['plan_run'])[:19])}</td></tr>")

    fault_html = ("<div class='warn'><b>Faults &mdash; fix before going live</b><ul>"
                  + "".join(f"<li>{f}</li>" for f in faults) + "</ul></div>") if faults else \
        ("<div class='ok'>No faults: every tick accounted for, the block was never armed by "
         "anything else, no plan went stale, and no discharge was decided under the floor.</div>")
    preview_html = ("<div class='legend'><div class='leg'><b>What going live would have "
                    "changed</b></div>"
                    + "".join(f"<div class='leg note' style='margin:0;padding:0;border:0'>"
                              f"{x}</div>" for x in previews)
                    + "</div>") if previews else ""

    legend = "".join(
        f"<div class='leg'><span class='dot' style='background:{COLOURS[k]}'></span>"
        f"<b>{k}</b> &mdash; {v}</div>" for k, v in DECISION_HELP.items())

    page = HTML.format(
        style=review_page.STYLE,
        chart=svg_chart(runs, battery, soc, price, t0, t1),
        legend=legend,
        faults=fault_html,
        previews=preview_html,
        rows="".join(rows),
        ticks=len(ticks),
        decisions=len(runs),
        mix=", ".join(f"{k} {v}" for k, v in sorted(tick_counts.items())),
        kinds=", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        plans=len(plan_runs),
        window=f"{local(t0):%a %d %b %H:%M} &ndash; {local(t1):%H:%M}",
        floor=f"{a.soc_floor:.0f}",
        tz=TZ_LABEL,
        generated=f"{local(now):%Y-%m-%d %H:%M}",
    )
    out.write_text(page)
    print(f"{len(ticks)} ticks, {len(runs)} decisions, {len(faults)} faults, "
          f"{len(previews)} previews -> {out}")
    return 0


HTML = """<title>Dry Run Decisions</title>
{style}<div class="wrap">

<header class="page">
  <div class="eyebrow">dry run &middot; decisions vs actual</div>
  <h1>Dry run decisions</h1>
  <p class="sub">Every decision the dispatcher made while writing nothing, against what the
  battery actually did. Read the bands against the dark SoC curve and the violet power trace:
  where they disagree is where going live would change the outcome. All times {tz}.</p>
</header>

<dl class="summary">
  <div class="ro"><dt>window</dt><dd>{window}</dd></div>
  <div class="ro"><dt>ticks</dt><dd>{ticks:,}</dd></div>
  <div class="ro"><dt>decisions</dt><dd>{decisions}</dd></div>
  <div class="ro"><dt>plans used</dt><dd>{plans}</dd></div>
  <div class="ro"><dt>tick mix</dt><dd>{mix}</dd></div>
  <div class="ro"><dt>soc floor</dt><dd>{floor}%</dd></div>
</dl>

{faults}

<section>
  <div class="rh">
    <h2>The day</h2>
    <p class="why">Bands are the decision. The dark line is <b>actual SoC</b>; the violet
    trace is <b>actual battery power</b>, charging positive, zero on the dashed midline. Bars
    below are the <b>buy price</b>, red where negative.</p>
  </div>
  {chart}
  <div class="legend">
  {legend}
  <div class="leg note">
  <b>The violet trace is not a command.</b> Nothing was commanded: dry run writes no
  registers, so the battery ran plain self-consumption all day and this is what that produced.
  The published <code>setpoint_w</code> is a readback of an idle block and reads 0 throughout,
  which is why this page is built on <code>slot_action</code> instead &mdash; the decision,
  which <code>state.py</code> publishes from the slot rather than from the registers.
  </div>
  </div>
</section>

{previews}

<section>
  <div class="rh"><h2>Every decision</h2>
  <p class="why">One row per contiguous run of the same decision. <b>Actual</b> is the mean
  battery power over that window, charging positive.</p></div>
  <div class="tablewrap">
  <table>
    <tr><th>from</th><th>to</th><th>decision</th><th class="n">min</th>
        <th class="n">actual</th><th class="n">soc</th><th>plan run</th></tr>
    {rows}
  </table>
  </div>
</section>

<footer>Generated {generated} by <b>scripts/review-dry-run.py</b> from the
<code>alphaess</code> bucket. Nothing here is written back. This page answers
DISPATCH-GOLIVE.md section 3&rsquo;s fourth box, which is the last chance to catch a wrong
decision for free &mdash; after <code>DISPATCH_LIVE=1</code> the mistakes cost electricity.
</footer>
</div>
"""


if __name__ == "__main__":
    sys.exit(main())
