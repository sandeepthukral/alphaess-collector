"""The shared look of the review pages, and the small helpers that draw them.

Two scripts publish review artefacts -- `review-plan-corpus.py` (plan -> slots, offline, from
the snapshot in dispatch/testdata) and `review-dry-run.py` (what the running dispatcher
decided, from InfluxDB). They are different questions over different data and stay separate
programs, but they are read by the same person on the same phone, and a reader who has learnt
that violet means "commanded power" on one page must not find it meaning something else on
the other.

So the stylesheet and the colour vocabulary live here, in one copy. This is the same argument
the `capacity_wh` work made in PLAN-repo-seams.md 2a: a constant duplicated across two
consumers is not a constant, it is two constants that agree until the day they do not.

Module name is underscored, unlike its two callers, because it is imported rather than run.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatch"))

from plan import PLANNER_TZ

# Everything on these pages is drawn in the planner's own wall clock. The control path is UTC
# instants and stays that way -- section 4.3 requires it -- but a REVIEW is a human deciding
# whether a command lands at the evening peak, and nobody knows where the evening peak is in
# UTC. The two differ by an hour or two depending on the season, which is exactly enough to
# make a wrong chart look right.
DISPLAY_TZ = PLANNER_TZ
TZ_LABEL = "Europe/Amsterdam"

# The action vocabulary, shared so a band means the same thing on both pages.
COLOURS = {
    "charge": "#3b82f6",
    "discharge": "#f97316",
    "self": "#10b981",
    "hold": "#94a3b8",
}
BAND_HELP = {
    "charge": "forced charge, Mode 2, power + SoC target written",
    "discharge": "forced discharge, Mode 2, power + SoC target written",
    "self": "NO command -- dispatch released, plain self-consumption",
    "hold": "Mode 3 at 0 W -- battery frozen, surplus exported",
}


def parse_ts(s: str) -> dt.datetime:
    """An ISO instant, `Z` or offset. Never a string compare -- see plan.run_sort_key."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def local(t: dt.datetime) -> dt.datetime:
    return t.astimezone(DISPLAY_TZ)


def hour_step(px_per_hour: float) -> tuple[int, bool]:
    """How often to label the time axis, and whether half hours get a tick.

    Driven by the pixels actually available rather than fixed at every hour: the corpus holds
    horizons from a few hours to ~36, and a step that is right for one is unreadable overlap
    or bare acreage for the other.

    The threshold is 22 px because the labels are bare hour numbers -- two monospace digits,
    about 13 px, plus a gap. Writing them as `18:00` would need 32 px and force a 36-hour
    horizon onto a two-hourly axis, which is the resolution the reader asked not to have.
    """
    for step in (1, 2, 3, 6, 12):
        if px_per_hour * step >= 22:
            return step, px_per_hour >= 26
    return 24, False


STYLE = """\
<style>
/* An instrument readout, not a document. The display face is monospace because every
   identifier on this page already is -- run tags, timestamps, setpoints, register values --
   and a proportional heading above a monospace body would be the only thing here pretending
   to be prose. Neutrals carry a slight green bias, borrowed from the phosphor of the panel
   meters this replaces, so the greys read as chosen rather than inherited. */
:root {
  --ground:#f6f8f6; --panel:#ffffff; --ink:#0e1613; --muted:#5d6f68; --rule:#dae2dd;
  --grid:#e6ece8; --accent:#0f766e;
  --soc:#0e1613; --pow:#7c3aed; --price:#0d9488;
  --warn-bg:#fdf3e3; --warn-ink:#7c4a13; --warn-rule:#e8cfa4;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#080c0b; --panel:#0f1614; --ink:#dfe8e3; --muted:#8ba099; --rule:#1e2926;
    --grid:#18211f; --accent:#2dd4bf;
    --soc:#f2f7f5; --pow:#c4b5fd; --price:#2dd4bf;
    --warn-bg:#241a0b; --warn-ink:#f0c27a; --warn-rule:#4a3617;
  }
}
:root[data-theme="dark"] {
  --ground:#080c0b; --panel:#0f1614; --ink:#dfe8e3; --muted:#8ba099; --rule:#1e2926;
  --grid:#18211f; --accent:#2dd4bf;
  --soc:#f2f7f5; --pow:#c4b5fd; --price:#2dd4bf;
  --warn-bg:#241a0b; --warn-ink:#f0c27a; --warn-rule:#4a3617;
}

* { box-sizing:border-box; }
body {
  background:var(--ground); color:var(--ink); margin:0; padding:0 20px 72px;
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-variant-numeric:tabular-nums;
}
.wrap { max-width:1180px; margin:0 auto; display:flex; flex-direction:column; gap:26px; }

header.page { padding:36px 0 0; display:flex; flex-direction:column; gap:10px; }
.eyebrow {
  font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:0.14em; text-transform:uppercase; color:var(--accent);
}
h1 {
  font:600 27px/1.15 ui-monospace,SFMono-Regular,Menlo,monospace;
  margin:0; letter-spacing:-0.02em; text-wrap:balance;
}
.sub { color:var(--muted); margin:0; max-width:66ch; }

.summary {
  display:flex; flex-wrap:wrap; gap:0; border:1px solid var(--rule);
  border-radius:4px; background:var(--panel); overflow:hidden;
}
.summary .ro { flex:1 1 128px; padding:12px 16px; border-right:1px solid var(--rule); }
.summary .ro:last-child { border-right:0; }

.ro dt {
  font:600 10.5px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:0.09em; text-transform:uppercase; color:var(--muted); margin:0 0 5px;
}
.ro dd { margin:0; font:15px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; }

.legend {
  border:1px solid var(--rule); border-left:2px solid var(--accent);
  border-radius:4px; background:var(--panel); padding:16px 18px;
  display:flex; flex-direction:column; gap:7px;
}
.leg { font-size:13.5px; }
.leg b { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-weight:600; }
.leg.note { color:var(--muted); margin-top:6px; padding-top:11px;
  border-top:1px solid var(--rule); max-width:78ch; }
.dot { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:7px; vertical-align:-1px; }

section {
  border:1px solid var(--rule); border-radius:4px; background:var(--panel);
  padding:18px 20px 14px; display:flex; flex-direction:column; gap:14px;
}
.rh { display:flex; flex-direction:column; gap:3px; }
h2 { font:600 16px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; margin:0; }
.why { color:var(--muted); margin:0; font-size:13px; }

.readouts { display:flex; flex-wrap:wrap; gap:10px 30px; margin:0;
  padding-bottom:2px; border-bottom:1px solid var(--rule); }
.readouts .ro { padding:0 0 10px; }

.chart { width:100%; height:auto; display:block; }
text.ax { fill:var(--muted); font-size:11px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
/* The date line under the hours: quieter than the hour labels it annotates, because a
   reader looking for 18:00 should not have to read past a day name to find it. */
text.ax.day { font-size:10px; letter-spacing:0.06em; opacity:0.75; }

/* Per-chart key. Sits above the chart so it is read before the traces, not after. */
.key { display:flex; flex-wrap:wrap; align-items:center; gap:6px 18px;
  font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); }
.key .k { display:inline-flex; align-items:center; gap:7px; }
.key.bands { gap:6px 12px; }
.key.bands .k { border:1px solid var(--rule); border-radius:99px; padding:4px 10px 4px 8px; }
.klabel { font-size:10.5px; letter-spacing:0.09em; text-transform:uppercase; opacity:0.8; }
svg.sw { width:24px; height:10px; flex:none; overflow:visible; }

.warn {
  background:var(--warn-bg); color:var(--warn-ink); border:1px solid var(--warn-rule);
  border-radius:4px; padding:11px 15px; font-size:13.5px;
}
.warn ul { margin:7px 0 0; padding-left:19px; }
.ok { color:var(--muted); font-size:12.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }

details { border-top:1px solid var(--rule); padding-top:11px; }
.stalls { margin:9px 0 2px; padding-left:19px; color:var(--muted); font-size:13px; }
.stalls li { margin:5px 0; }
summary { cursor:pointer; color:var(--muted); font-size:13px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
summary:focus-visible { outline:2px solid var(--accent); outline-offset:3px; border-radius:2px; }
.tablewrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; margin-top:12px;
  font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
th, td { text-align:left; padding:4px 14px 4px 0;
  border-bottom:1px solid var(--grid); white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:11px;
  letter-spacing:0.07em; text-transform:uppercase; }
th.n, td.n { text-align:right; }

footer { color:var(--muted); font-size:12.5px; max-width:74ch;
  border-top:1px solid var(--rule); padding-top:16px; }
</style>
"""
