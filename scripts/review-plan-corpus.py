#!/usr/bin/env python3
"""Translate the reviewed corpus runs into slots, and draw them. PLAN-repo-seams.md 5d.

    scripts/review-plan-corpus.py            # the 8 selected runs
    scripts/review-plan-corpus.py --all      # every run in the corpus

Writes `dispatch/testdata/review/slots_<run>.json` and one self-contained `review.html`.

WHY THIS EXISTS AT ALL. A plan table is 130 rows of numbers and a slots file is a list of
instants; neither is reviewable by a human, and a golden committed without review just freezes
whatever the code did on the day. The chart is the review: SoC trajectory, the slot bands
under it, and the commanded power step line together make a wrong translation LOOK wrong --
a discharge band where SoC is climbing, a charge command at the evening peak.

Output goes under `dispatch/testdata/`, which is gitignored, because these derive from
household plan data. Goldens get promoted out of here deliberately, after review -- never by
this script.

Self-contained SVG, no libraries: it is published as an artifact and read on a phone.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dispatch"))

import corpus  # noqa: E402
import review_page  # noqa: E402
from plan import from_table, interval_minutes  # noqa: E402
from translator import build_document  # noqa: E402

# The display timezone, the action colours and the stylesheet are shared with
# `review-dry-run.py`. See review_page.py for why they are not kept in two copies -- briefly,
# the two pages are read by the same person, and violet must not mean commanded power on one
# and something else on the other.
DISPLAY_TZ, TZ_LABEL = review_page.DISPLAY_TZ, review_page.TZ_LABEL
COLOURS, BAND_HELP = review_page.COLOURS, review_page.BAND_HELP

W, H_MAIN, H_PRICE, PAD_L, PAD_R, PAD_T = 1120, 250, 90, 52, 20, 14

# Swatches for the per-chart key. Each one is drawn the way the trace itself is drawn -- a
# flat line for SoC, an actual step for commanded power, bars for price -- so the key can be
# matched to the chart by shape and not only by colour. That matters for the two dark traces,
# which are told apart by shape long before anyone reads the label.
LINE_SWATCH = ('<svg class="sw" viewBox="0 0 24 10" aria-hidden="true">'
               '<line x1="1" y1="5" x2="23" y2="5" stroke="var(--soc)" stroke-width="2.2"/>'
               "</svg>")
STEP_SWATCH = ('<svg class="sw" viewBox="0 0 24 10" aria-hidden="true">'
               '<polyline points="1,8 8,8 8,2 16,2 16,6 23,6" fill="none" '
               'stroke="var(--pow)" stroke-width="1.8"/></svg>')
BAR_SWATCH = ('<svg class="sw" viewBox="0 0 24 10" aria-hidden="true">'
              '<rect x="2" y="4" width="3" height="6" fill="var(--price)"/>'
              '<rect x="7" y="2" width="3" height="8" fill="var(--price)"/>'
              '<rect x="12" y="5" width="3" height="5" fill="var(--price)"/>'
              '<rect x="17" y="1" width="3" height="9" fill="var(--price)"/></svg>')


_t, _loc = review_page.parse_ts, review_page.local
hour_step = review_page.hour_step


def svg_chart(intervals, doc, capacity_wh: float) -> tuple[str, dict]:
    """One run: price bars, planned SoC, slot bands, commanded power.

    Returns the SVG and the scales it chose, so the per-chart key can state the actual
    numbers rather than repeating a generic legend.
    """
    ivs = sorted(intervals, key=lambda i: i.start)
    span = dt.timedelta(minutes=interval_minutes(ivs))
    t0, t1 = ivs[0].start, ivs[-1].start + span
    total = (t1 - t0).total_seconds()
    plot_w = W - PAD_L - PAD_R

    def x(t: dt.datetime) -> float:
        return PAD_L + plot_w * (t - t0).total_seconds() / total

    y_main_0, y_main_1 = PAD_T, PAD_T + H_MAIN
    def y_soc(pct: float) -> float:
        return y_main_1 - H_MAIN * pct / 100.0

    powers = [s.get("power_w", 0) or 0 for s in doc["slots"]]
    pmax = max([*(abs(p) for p in powers), 1000])
    def y_pow(w: float) -> float:
        # Shares the main panel, zero on the mid-line, so a command can be read against the
        # SoC curve above it without flipping between two charts.
        return (y_main_0 + y_main_1) / 2 - (H_MAIN / 2) * (w / pmax)

    y_pr_0, y_pr_1 = y_main_1 + 38, y_main_1 + 38 + H_PRICE
    prices = [i.price_buy for i in ivs]
    pr_hi, pr_lo = max([*prices, 0.01]), min([*prices, 0.0])
    pr_rng = (pr_hi - pr_lo) or 1.0
    def y_price(p: float) -> float:
        return y_pr_1 - H_PRICE * (p - pr_lo) / pr_rng

    o: list[str] = [f'<svg viewBox="0 0 {W} {y_pr_1 + 48}" class="chart" '
                    f'xmlns="http://www.w3.org/2000/svg">']

    # 1. Slot bands first, so everything else draws over them.
    for s in doc["slots"]:
        x0, x1 = x(_t(s["start"])), x(_t(s["end"]))
        o.append(f'<rect x="{x0:.1f}" y="{y_main_0}" width="{max(x1 - x0, 0.6):.1f}" '
                 f'height="{H_MAIN}" fill="{COLOURS[s["action"]]}" opacity="0.16"/>')

    # 2. Gridlines and the midline that power is measured from.
    for pct in (0, 25, 50, 75, 100):
        yy = y_soc(pct)
        o.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{W - PAD_R}" y2="{yy:.1f}" '
                 f'stroke="var(--grid)" stroke-width="1"/>')
        o.append(f'<text x="{PAD_L - 8}" y="{yy + 4:.1f}" class="ax" '
                 f'text-anchor="end">{pct}%</text>')
    ymid = (y_main_0 + y_main_1) / 2
    o.append(f'<line x1="{PAD_L}" y1="{ymid:.1f}" x2="{W - PAD_R}" y2="{ymid:.1f}" '
             f'stroke="var(--ink)" stroke-width="1" stroke-dasharray="2 3" opacity="0.45"/>')

    # 3. The time axis. Hours are the unit the plan is argued in -- "why is it charging at
    #    14:00" is the question the review exists to answer -- so hours get the labels and
    #    midnight gets the heavy rule that separates one day from the next.
    px_per_hour = plot_w / (total / 3600.0)
    step, half_ticks = hour_step(px_per_hour)

    # Start from the first whole hour at or after t0, in LOCAL time, so labels land on :00 and
    # not on whatever minute the horizon happens to begin.
    first = _loc(t0).replace(minute=0, second=0, microsecond=0)
    if first < _loc(t0):
        first += dt.timedelta(hours=1)

    t, y_ticks = first, y_pr_1
    while t.astimezone(dt.UTC) <= t1:
        utc = t.astimezone(dt.UTC)
        xx, midnight = x(utc), t.hour == 0
        # A label every `step` hours, but midnight is always labelled: it is the one tick that
        # carries a date, and skipping it because it fell off the step would leave a day
        # boundary the reader cannot name.
        if midnight or t.hour % step == 0:
            o.append(f'<line x1="{xx:.1f}" y1="{y_main_0}" x2="{xx:.1f}" y2="{y_ticks}" '
                     f'stroke="var(--ink)" stroke-width="1" '
                     f'opacity="{0.28 if midnight else 0.10}"/>')
            o.append(f'<line x1="{xx:.1f}" y1="{y_ticks}" x2="{xx:.1f}" y2="{y_ticks + 5}" '
                     f'stroke="var(--ink)" stroke-width="1" opacity="0.5"/>')
            o.append(f'<text x="{xx:.1f}" y="{y_ticks + 18}" class="ax" '
                     f'text-anchor="middle">{t:%H}</text>')
            if midnight:
                o.append(f'<text x="{xx + 4:.1f}" y="{y_ticks + 34}" class="ax day">'
                         f'{t:%a %d %b}</text>')
        elif half_ticks or t.hour % max(step // 2, 1) == 0:
            o.append(f'<line x1="{xx:.1f}" y1="{y_ticks}" x2="{xx:.1f}" y2="{y_ticks + 3}" '
                     f'stroke="var(--ink)" stroke-width="1" opacity="0.3"/>')
        if half_ticks:
            hx = x((t + dt.timedelta(minutes=30)).astimezone(dt.UTC))
            if hx <= W - PAD_R:
                o.append(f'<line x1="{hx:.1f}" y1="{y_ticks}" x2="{hx:.1f}" y2="{y_ticks + 3}" '
                         f'stroke="var(--ink)" stroke-width="1" opacity="0.3"/>')
        # Stepping in UTC and re-localising keeps the DST night honest: on 2026-10-25 the
        # local hour repeats, and an axis walked in local time would draw 02:00 twice at the
        # same x. This walks real elapsed time and lets the label repeat where it truly does.
        t = _loc(utc + dt.timedelta(hours=1))

    # The leading partial day has no midnight of its own to carry a date, so it gets one here
    # -- unless midnight falls close enough that the two labels would sit on top of each other.
    first_midnight = _loc(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    if first_midnight < _loc(t0):
        first_midnight += dt.timedelta(days=1)
    if x(first_midnight.astimezone(dt.UTC)) - PAD_L > 150:
        o.append(f'<text x="{PAD_L}" y="{y_ticks + 34}" class="ax day">'
                 f'{_loc(t0):%a %d %b}</text>')
    o.append(f'<text x="{W - PAD_R}" y="{y_ticks + 34}" class="ax day" text-anchor="end">'
             f'hours &#183; {TZ_LABEL}</text>')

    # 4. Commanded power, as a step. Charging positive, matching every other panel in this
    #    repo (see registers.py on why the sign flips exactly once, at the encoder).
    pts: list[str] = []
    for s in doc["slots"]:
        w = s.get("power_w") or 0
        if s["action"] == "discharge":
            w = -w
        elif s["action"] not in ("charge",):
            w = 0
        pts += [f"{x(_t(s['start'])):.1f},{y_pow(w):.1f}",
                f"{x(_t(s['end'])):.1f},{y_pow(w):.1f}"]
    o.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="var(--pow)" '
             f'stroke-width="1.6" opacity="0.95"/>')

    # 5. Planned SoC. Plotted at each interval's END, because that is what soc_wh means
    #    (plan.py: the LP constraint at Marstek-planning.py:2006-2008). Plotting it at the
    #    start would shift the whole curve one interval early and still look plausible.
    xy = [(x(i.start + span), y_soc(100 * i.soc_wh / capacity_wh)) for i in ivs]
    soc_pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in xy)
    o.append(f'<polygon points="{x(ivs[0].start):.1f},{y_main_1} {soc_pts} '
             f'{xy[-1][0]:.1f},{y_main_1}" fill="var(--soc)" opacity="0.07"/>')
    o.append(f'<polyline points="{soc_pts}" fill="none" stroke="var(--soc)" '
             f'stroke-width="2.2"/>')
    # The endpoint is where the plan says the horizon leaves the battery, which is the number
    # the next run re-anchors from -- worth being able to find without counting gridlines.
    o.append(f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="3.2" fill="var(--soc)"/>')

    # 6. Price panel.
    bw = max(plot_w / max(len(ivs), 1) - 0.6, 0.8)
    zero_y = y_price(0.0) if pr_lo < 0 < pr_hi else y_pr_1
    for i in ivs:
        yy = y_price(i.price_buy)
        top, hgt = min(yy, zero_y), abs(yy - zero_y)
        o.append(f'<rect x="{x(i.start):.1f}" y="{top:.1f}" width="{bw:.2f}" '
                 f'height="{max(hgt, 0.7):.1f}" '
                 f'fill="{"#ef4444" if i.price_buy < 0 else "var(--price)"}" opacity="0.75"/>')
    if pr_lo < 0 < pr_hi:
        o.append(f'<line x1="{PAD_L}" y1="{zero_y:.1f}" x2="{W - PAD_R}" y2="{zero_y:.1f}" '
                 f'stroke="#ef4444" stroke-width="1"/>')
    o.append(f'<text x="{PAD_L - 8}" y="{y_pr_0 + 10}" class="ax" text-anchor="end">'
             f'{pr_hi:.2f}</text>')
    o.append(f'<text x="{PAD_L - 8}" y="{y_pr_1}" class="ax" text-anchor="end">'
             f'{pr_lo:.2f}</text>')
    o.append(f'<text x="{PAD_L}" y="{y_pr_0 - 8}" class="ax">buy price EUR/kWh</text>')
    o.append(f'<text x="{W - PAD_R}" y="{y_main_0 + 12}" class="ax" text-anchor="end">'
             f'power scale +/-{pmax} W</text>')
    o.append("</svg>")
    return "\n".join(o), {"pmax": pmax, "price_hi": pr_hi, "price_lo": pr_lo}


def run_section(intervals, meta, capacity_wh, floor) -> tuple[str, dict]:
    doc, warnings = build_document(
        intervals, capacity_wh,
        generated_at=dt.datetime.now(dt.UTC), floor=floor)

    counts: dict[str, int] = {}
    for s in doc["slots"]:
        counts[s["action"]] = counts.get(s["action"], 0) + 1

    tag = html.escape(meta["plan_run"])
    reason = html.escape(meta["features"].get("selection_reason", "") or "not selected")
    f = meta["features"]

    rows = "".join(
        f"<tr><td>{_loc(_t(s['start'])):%a %H:%M}</td><td>{_loc(_t(s['end'])):%H:%M}</td>"
        f"<td><span class='dot' style='background:{COLOURS[s['action']]}'></span>"
        f"{s['action']}</td>"
        f"<td class='n'>{s.get('power_w', '') or ''}</td>"
        f"<td class='n'>{s.get('target_soc', '') if s.get('target_soc') is not None else ''}</td>"
        f"</tr>"
        for s in doc["slots"])

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        warn_html = f"<div class='warn'><b>{len(warnings)} warning(s)</b><ul>{items}</ul></div>"
    else:
        warn_html = "<div class='ok'>no warnings</div>"

    def readout(label: str, value: str) -> str:
        return f'<div class="ro"><dt>{label}</dt><dd>{value}</dd></div>'

    readouts = "".join([
        readout("intervals", str(len(intervals))),
        readout("cadence", f"{f['interval_minutes']} min"),
        readout("horizon", f"{f['horizon_hours']} h"),
        readout("SoC range", f"{f['min_soc_pct']}&ndash;{f['max_soc_pct']} %"),
        readout("slots", str(len(doc["slots"]))),
        readout("charge", f"{f['total_charge_wh']:,} Wh"),
        readout("discharge", f"{f['total_discharge_wh']:,} Wh"),
    ])

    chart, scale = svg_chart(intervals, doc, capacity_wh)

    # The key sits ABOVE each chart rather than once at the top of the page. On a phone the
    # page-level legend scrolls out of sight long before the first chart is on screen, which
    # leaves two unlabelled traces -- and the whole point of the review is that a reader can
    # tell the SoC curve from the commanded power at a glance.
    key = "".join([
        f'<span class="k">{LINE_SWATCH}planned SoC &mdash; % of {capacity_wh / 1000:.1f} kWh, '
        f'at each interval&rsquo;s end</span>',
        f'<span class="k">{STEP_SWATCH}commanded power &mdash; '
        f'&plusmn;{scale["pmax"]:,} W, charging up</span>',
        f'<span class="k">{BAR_SWATCH}buy price &mdash; '
        f'{scale["price_lo"]:.2f} to {scale["price_hi"]:.2f} EUR/kWh</span>',
    ])
    bands = "".join(
        f'<span class="k"><span class="dot" style="background:{COLOURS[k]}"></span>'
        f'{k} &times;{v}</span>'
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))

    section = f"""
<section>
  <header class="rh">
    <h2>{tag}</h2>
    <p class="why">{reason}</p>
  </header>
  <dl class="readouts">{readouts}</dl>
  <div class="key traces">{key}</div>
  <div class="key bands"><span class="klabel">bands</span>{bands}</div>
  {chart}
  {warn_html}
  <details>
    <summary>slot table &mdash; {len(doc['slots'])} rows</summary>
    <div class="tablewrap">
      <table><thead><tr><th>start</th><th>end</th><th>action</th><th class="n">W</th>
      <th class="n">target %</th></tr></thead><tbody>{rows}</tbody></table>
    </div>
  </details>
</section>"""
    return section, doc


def plan_file_run(path: Path, capacity_wh: float) -> tuple[list, dict]:
    """A planner table straight off the NAS, shaped like a corpus run so it renders the same.

    The corpus comes from InfluxDB because that is the production read path. A table file is
    the other thing the planner emits, and being able to drop one onto the page matters for
    the newest plan of all: Influx has it only after the hourly writer runs, while
    `plans/plan_YYYYMMDD_HH.txt` exists the moment the planner finishes.

    `plan_run` is taken from the filename rather than invented, so the tag on the page is the
    plan the NAS actually named.
    """
    # The file's own stem is the tag. Not a synthesised ISO timestamp: the hour in the name
    # is the planner's LOCAL run hour, so rendering it as `...T23:00` would look like an
    # instant and be wrong by the UTC offset. It also keeps the derived slots filename free
    # of spaces and punctuation.
    tag = path.stem                                     # plan_20260815_23
    ivs = from_table(path.read_text(), plan_run=tag)
    socs = [i.soc_wh for i in ivs]
    span_h = (ivs[-1].start - ivs[0].start).total_seconds() / 3600
    return ivs, {
        "plan_run": tag,
        "features": {
            "selection_reason": f"planner table file from the NAS -- {path.name}",
            "interval_minutes": interval_minutes(ivs),
            "horizon_hours": round(span_h, 2),
            "min_soc_pct": round(100 * min(socs) / capacity_wh, 1),
            "max_soc_pct": round(100 * max(socs) / capacity_wh, 1),
            "total_charge_wh": round(sum(i.charge_wh for i in ivs)),
            "total_discharge_wh": round(sum(i.discharge_wh for i in ivs)),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--all", action="store_true", help="Every corpus run, not just the selected")
    p.add_argument("--plan", action="append", default=[], metavar="FILE",
                   help="Also render a planner table file (repeatable). Rendered first.")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    manifest_path = corpus.TESTDATA_DIR / "manifest.json"
    if not manifest_path.exists():
        sys.exit("no corpus -- run scripts/fetch-plan-corpus.py first")
    manifest = json.loads(manifest_path.read_text())
    capacity_wh = manifest["capacity_wh"]
    floor = manifest["energy_floor_wh"]

    loaded = corpus.load_all()
    if not a.all:
        loaded = [x for x in loaded if x[1]["features"].get("selected_for_review")]
    # Table files go first: they are the newest thing on the page, and the reason for asking
    # for one is to look at it.
    loaded = [plan_file_run(Path(f), capacity_wh) for f in a.plan] + loaded
    if not loaded:
        sys.exit("no runs to review")

    out = Path(a.out) if a.out else corpus.TESTDATA_DIR / "review"
    (out / "slots").mkdir(parents=True, exist_ok=True)

    sections, total_warnings = [], 0
    for intervals, meta in loaded:
        sec, doc = run_section(intervals, meta, capacity_wh, floor)
        sections.append(sec)
        total_warnings += sec.count("<li>")
        name = corpus.run_filename(meta["plan_run"]).replace("run_", "slots_")
        (out / "slots" / name).write_text(json.dumps(doc, indent=1))
        print(f"{meta['plan_run']}  {len(doc['slots']):>3} slots -> slots/{name}")

    legend = "".join(
        f"<div class='leg'><span class='dot' style='background:{c}'></span>"
        f"<b>{k}</b> &mdash; {BAND_HELP[k]}</div>" for k, c in COLOURS.items())

    page = HTML.format(
        style=review_page.STYLE,
        legend=legend,
        sections="\n".join(sections),
        n=len(loaded),
        warnings=total_warnings,
        fetched=html.escape(manifest["fetched_at"]),
        capacity=int(capacity_wh),
        floor=int(floor),
        runs=manifest["run_count"],
        points=manifest["point_count"],
        tz=TZ_LABEL,
    )
    (out / "review.html").write_text(page)
    print(f"\n{out / 'review.html'}")
    return 0


HTML = """<title>Dispatch Slot Review</title>
{style}<div class="wrap">

<header class="page">
  <div class="eyebrow">plan &rarr; slots</div>
  <h1>Dispatch slot review</h1>
  <p class="sub">Each plan run below is translated into the dispatch slots the scheduler
  would act on. Read the bands against the SoC curve: a discharge band under a rising curve,
  or a charge band at the evening peak, is the translation getting it wrong.
  All times {tz}.</p>
</header>

<dl class="summary">
  <div class="ro"><dt>runs shown</dt><dd>{n}</dd></div>
  <div class="ro"><dt>corpus</dt><dd>{runs} runs</dd></div>
  <div class="ro"><dt>points</dt><dd>{points:,}</dd></div>
  <div class="ro"><dt>warnings</dt><dd>{warnings}</dd></div>
  <div class="ro"><dt>capacity</dt><dd>{capacity:,} Wh</dd></div>
  <div class="ro"><dt>energy floor</dt><dd>{floor} Wh</dd></div>
  <div class="ro"><dt>fetched</dt><dd>{fetched}</dd></div>
</dl>

<div class="legend">
{legend}
<div class="leg note">
The dark trace is the plan&rsquo;s <b>SoC trajectory</b>, plotted at each interval&rsquo;s END
&mdash; that is what <b>soc_wh</b> means, and plotting it at the start would shift the whole
curve one interval early while still looking plausible. The violet step is the
<b>commanded power</b>, charging positive, zero on the dashed midline. Bars below are the
<b>buy price</b>; red bars are negative prices.
</div>
</div>

{sections}

<footer>Generated by <b>scripts/review-plan-corpus.py</b> from the snapshot in
dispatch/testdata/, which is gitignored. Slot documents are written beside this page.
Goldens are promoted out of testdata only after these charts have been reviewed &mdash;
committing one unreviewed would freeze whatever the translator happened to do that day.
</footer>
</div>
"""


if __name__ == "__main__":
    sys.exit(main())
