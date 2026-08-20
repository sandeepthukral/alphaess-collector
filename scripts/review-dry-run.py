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
# `dispatch/` too, and flat, which is how those modules import each other and how the
# Dockerfile lays them out in /app -- so this script imports exactly what runs in production.
sys.path.insert(0, str(REPO / "dispatch"))

import review_page  # noqa: E402
from reliability import (  # noqa: E402
    DEFAULT_SOC_FLOOR,
    FIELDS,
    STALE_PLAN_S,
    TICK_S,
    analyse,
    by_severity,
    decision_runs,
    mean_between,
)
from review_page import COLOURS, TZ_LABEL, hour_step, local  # noqa: E402

# The thresholds live in `dispatch/reliability.py` and are imported above, not restated
# here. They were declared twice -- once in this file and once in `is-it-deciding.py` --
# bound only by a comment saying they had to match, which is the arrangement
# `review_page.py`'s own docstring argues against. Two scripts disagreeing about what counts
# as a stalled loop is a worse failure than either number being slightly wrong.

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


def _hhmm(t) -> str:
    return f"{local(t):%H:%M}"


def render(f) -> str:
    """One finding as the sentence the page shows.

    Rendering is HERE and the judgement is in `reliability.py`, which is the whole point of
    the split: this function decides how alarming something LOOKS, and cannot accidentally
    decide whether it IS a fault. Every branch reads the numbers out of `detail` -- nothing
    is re-derived, so the page and the nightly rollup can never describe the same window
    differently.
    """
    d = f.detail

    if f.kind == "gap":
        where = f"{_hhmm(d['start'])} &rarr; {_hhmm(d['end'])}"
        mins = d["gap_s"] / 60
        if d["cause"] == "unknown":
            return (f"<b>{mins:.0f} min with no decision</b>, {where}. The loop ticks every "
                    f"{TICK_S} s, so this is either the loop or the network &mdash; and "
                    f"there is no <code>power_readings</code> data in this window to tell "
                    f"them apart")
        if d["cause"] == "network":
            return (f"<b>{mins:.0f} min</b>, {where}. The collector stopped sampling around "
                    f"the same time, for {d['hole_s'] / 60:.0f} min, and it reaches the "
                    f"AlphaESS cloud over the WAN while dispatch reaches the inverter over "
                    f"the LAN &mdash; so this is upstream of both, not the dispatch loop")
        return (f"<b>{mins:.0f} min with no decision</b>, {where}. The collector kept "
                f"sampling either side of it, so the network was up and <b>this one is the "
                f"dispatch loop</b>. In dry run nothing else shows it &mdash; "
                f"<code>action</code> reads <code>no dispatch</code> either way")

    if f.kind == "armed":
        return (f"<b>{d['ticks']} tick(s) found the dispatch block ARMED</b> "
                f"({', '.join(html.escape(k) for k in d['kinds'])}). Dry run writes nothing, "
                f"so this dispatcher did not do it: something else is driving the registers "
                f"&mdash; the app&rsquo;s price control, or a leftover command. This is "
                f"monitor #7&rsquo;s case and it blocks going live")

    if f.kind == "discharge_below_floor":
        return (f"<b>discharge decided at {d['soc_pct']:.1f}% SoC</b>, below the "
                f"{d['floor_pct']:.0f}% floor, {_hhmm(d['start'])}&ndash;{_hhmm(d['end'])}. "
                f"<code>slots.decide()</code>&rsquo;s direction guard should have refused "
                f"this")

    if f.kind == "stale_plan":
        return (f"<b>{d['ticks']} tick(s) acted on a plan over "
                f"{STALE_PLAN_S / 3600:.0f} h old</b>, worst {d['worst_s'] / 3600:.1f} h. "
                f"The planner runs hourly, so this means it stopped &mdash; monitor #3")

    if f.kind == "blind":
        errs = ", ".join(f"<code>{html.escape(e)}</code>" for e in d["errors"])
        more = "" if d["distinct"] <= len(d["errors"]) else \
            f" and {d['distinct'] - len(d['errors'])} other(s)"
        return (f"<b>{d['ticks']} tick(s) could not read the inverter</b> ({errs}{more}). "
                f"The loop was alive and deciding throughout &mdash; a degraded tick "
                f"publishes its decision and no register readback, which is the fail-safe "
                f"working. Before <code>read_error</code> was queried these ticks were not "
                f"rows at all, and a run of them read as a gap")

    if f.kind == "divergence":
        return (f"{_hhmm(d['start'])}&ndash;{_hhmm(d['end'])} decided "
                f"<b>{d['action']}</b> ({d['wanted']}); the battery was actually doing "
                f"{d['actual_w']:+.0f} W. Going live would have overridden self-consumption "
                f"for {d['ticks']} min here")

    raise AssertionError(f"no renderer for finding kind {f.kind!r}")


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
    pr_hi = max([*prices, 0.01])
    # ZERO IS ONLY THE BASELINE WHEN THE DAY ACTUALLY GOES NEGATIVE. Anchoring at zero
    # unconditionally is what made the first version's price panel useless: a day of
    # 0.137-0.214 EUR/kWh rendered every bar at 64-100% of full height, so the panel filled
    # with near-identical blocks and the cheap hours -- the entire reason a price bar is on a
    # dispatch review -- were invisible. When every price is positive the floor is the
    # cheapest hour, padded so that hour still draws a visible stub rather than nothing.
    lo = min(prices) if prices else 0.0
    pr_lo = min(lo, 0.0) if lo < 0 else lo - 0.08 * (pr_hi - lo or 0.01)
    pr_rng = (pr_hi - pr_lo) or 1.0

    def y_price(p):
        return y_pr_1 - H_PRICE * (p - pr_lo) / pr_rng

    o = [f'<svg viewBox="0 0 {W} {y_pr_1 + 48}" class="chart" '
         f'xmlns="http://www.w3.org/2000/svg">']

    # BEFORE THE FIRST DECISION, THIS CHART IS NOT A REVIEW OF ANYTHING. The window is a whole
    # day but the dispatcher may have been publishing for only the last hour of it, and the
    # SoC and price traces are drawn across the whole span regardless -- they come from the
    # collector, which has been running for months. Left unmarked, 23 hours of untouched
    # battery read as "reviewed, nothing to report", which is the same lie the NO DISPATCHER
    # panel told: a page that looks like it is answering a question it never asked.
    if runs and runs[0]["start"] > t0:
        xb = x(runs[0]["start"])
        o.append(f'<rect x="{PAD_L}" y="{y_main_0}" width="{max(xb - PAD_L, 0):.1f}" '
                 f'height="{H_MAIN}" fill="var(--ink)" opacity="0.055"/>')
        o.append(f'<line x1="{xb:.1f}" y1="{y_main_0}" x2="{xb:.1f}" y2="{y_main_1}" '
                 f'stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/>')
        o.append(f'<text x="{xb - 7:.1f}" y="{y_main_0 + 13}" class="ax" '
                 f'text-anchor="end">no dispatcher &mdash; not reviewed</text>')

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

    # The price scale, stated rather than implied. Two labels, because the baseline is NOT
    # zero on an all-positive day (see above) and a bar chart with an unstated non-zero floor
    # invites exactly the wrong reading -- "twice as tall" would not mean "twice the price".
    if prices:
        o.append(f'<text x="{PAD_L - 8}" y="{y_price(pr_hi) + 9:.1f}" class="ax" '
                 f'text-anchor="end">{pr_hi:.2f}</text>')
        o.append(f'<text x="{PAD_L - 8}" y="{y_pr_1:.1f}" class="ax" '
                 f'text-anchor="end">{pr_lo:.2f}</text>')
        o.append(f'<text x="{PAD_L - 8}" y="{y_pr_1 + 13:.1f}" class="ax" '
                 f'text-anchor="end">&euro;/kWh</text>')
        if pr_lo < 0 < pr_hi:
            zy = y_price(0.0)
            o.append(f'<line x1="{PAD_L}" y1="{zy:.1f}" x2="{W - PAD_R}" y2="{zy:.1f}" '
                     f'stroke="var(--ink)" stroke-width="1" stroke-dasharray="2 3" '
                     f'opacity="0.45"/>')

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
        # `reliability.FIELDS`, not a list written out here. `read_error` joined it and
        # this call did not, which is how a degraded tick stayed invisible to the page.
        ticks = q_pivot(api, a.bucket, "dispatch_state", list(FIELDS), t0, t1)
        battery = q_series(api, a.bucket, "power_readings", "battery_power_w", t0, t1)
        soc = q_series(api, a.bucket, "power_readings", "soc_percent", t0, t1)
        # `market_price`, not `total`. Two reasons, and the first one is the important one:
        # it is the series the plan was optimised against and the one panel 2 of the dashboard
        # draws, so a decision questioned here is questioned against the number that caused
        # it. Second, `total` carries tax and markup, which is a near-constant additive
        # offset -- it compresses the visible spread to nothing and never goes negative, so
        # the red-bar case could not arise.
        price = q_series(api, a.bucket, "market_price", "market_price", t0, t1, every="1h")

    if not ticks:
        sys.exit("no dispatch_state points in that window -- is the dispatcher running?")

    runs = decision_runs(ticks)
    found = by_severity(analyse(ticks, runs, battery, soc, a.soc_floor))
    faults = [render(f) for f in found["fault"]]
    previews = [render(f) for f in found["preview"]]
    stalls = [render(f) for f in found["stall"]]
    degraded = [render(f) for f in found["degraded"]]
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
        (f"<div class='ok'>No faults: "
         f"{'every gap was a network stall' if stalls else 'every tick accounted for'}, the "
         f"block was never armed by anything else, no plan went stale, and no discharge was "
         f"decided under the floor.</div>")
    # Collapsed, and deliberately not styled as a warning. These are real -- the decisions
    # genuinely stopped -- but nothing in this repo can fix them, so they must not compete for
    # attention with the list above. Open by default would put the loudest block on the page
    # in front of the reader every morning the network was bad.
    stall_html = ("<details><summary>" + (
        f"{len(stalls)} network stall(s) &mdash; nothing to fix here"
    ) + "</summary><ul class='stalls'>"
        + "".join(f"<li>{s}</li>" for s in stalls) + "</ul></details>") if stalls else ""
    # Collapsed for the same reason as the stalls and NOT for the same reason as a fault:
    # an unreachable inverter is not a dispatch bug, and the loop having kept deciding
    # through it is the fail-safe working rather than damage to account for. It gets a block
    # at all because the alternative is what it had before -- nothing, and the ticks
    # misreported as a dead loop.
    degraded_html = ("<details><summary>" + (
        f"{len(degraded)} degraded tick block(s) &mdash; inverter unreadable, loop alive"
    ) + "</summary><ul class='stalls'>"
        + "".join(f"<li>{s}</li>" for s in degraded) + "</ul></details>") if degraded else ""
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
        stalls=stall_html,
        degraded=degraded_html,
        previews=preview_html,
        rows="".join(rows),
        ticks=len(ticks),
        decisions=len(runs),
        mix=", ".join(f"{k} {v}" for k, v in sorted(tick_counts.items())),
        kinds=", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        plans=len(plan_runs),
        window=f"{local(t0):%a %d %b %H:%M} &ndash; {local(t1):%H:%M}",
        # The window asked for and the window actually reviewed are different numbers whenever
        # the dispatcher started late, and the difference is the single most misleading thing
        # about this page if it is left to be inferred from the chart.
        covered=(f"{local(runs[0]['start']):%H:%M} &ndash; {local(runs[-1]['end']):%H:%M}"
                 f"{'' if runs[0]['start'] <= t0 + dt.timedelta(minutes=2) else ' only'}"),
        floor=f"{a.soc_floor:.0f}",
        tz=TZ_LABEL,
        generated=f"{local(now):%Y-%m-%d %H:%M}",
    )
    out.write_text(page)
    print(f"{len(ticks)} ticks, {len(runs)} decisions, {len(faults)} faults, "
          f"{len(stalls)} network stalls, {len(degraded)} degraded, "
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
  <div class="ro"><dt>decisions cover</dt><dd>{covered}</dd></div>
  <div class="ro"><dt>ticks</dt><dd>{ticks:,}</dd></div>
  <div class="ro"><dt>decisions</dt><dd>{decisions}</dd></div>
  <div class="ro"><dt>plans used</dt><dd>{plans}</dd></div>
  <div class="ro"><dt>tick mix</dt><dd>{mix}</dd></div>
  <div class="ro"><dt>soc floor</dt><dd>{floor}%</dd></div>
</dl>

{faults}

{stalls}

{degraded}

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
