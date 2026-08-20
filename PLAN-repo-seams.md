# Repo boundary and cross-repo seams — decision and deferred work

Status: **decided 2026-08-15.** Part 1 is done. Part 2a is done — the planner publishes
`capacity_wh` (battery-planning PR #25) and all three dashboards read it (PR #88, 2026-08-17).
Parts 2b and 3 are still deferred.

Prompted by the question of whether `alphaess-collector` and `battery-planning` should be
merged. They should not. This records why, and the work that follows from that answer.

---

## The decision

**Keep two repos.** `battery-planning` is a genuine GitHub fork of
`WillemD61/battery-planning`, which has **no licence** (`licenseInfo: null`) and is therefore
all-rights-reserved by default. `Marstek-planning.py` is 2,450 lines of that upstream, still
being refactored in place. The fork relationship is the attribution chain; copying the code
into this repo — an original project with no fork lineage — would sever it. That is the one
argument here with consequences outside the NAS.

The commit history says the same thing more cheaply: 20 active days each, only 5 overlapping,
all inside the July integration window, none since 2026-08-02. In August 2026: 71 commits
here, 8 there. They are not evolving in lockstep.

**But the dispatch feature belongs entirely in this repo.** `DESIGN-dispatch.md` §2 placed
the translator in `battery-planning` because "building this anywhere else means reimplementing
`advise.py`". That was written when the plan was read from a *file*. The later decision to
read the plan from **InfluxDB** dissolved the argument: a translator reading Influx needs none
of that repo's code, only its schema — which this repo's Grafana already consumes across 25
queries in `grafana/alphaess-battery-plan.json`. The `slots.json` seam was bridging a boundary
that no longer existed.

`slots.json` survives as an *internal* artefact. It is still worth having: a crash-surviving,
inspectable file decouples the translator from the 60 s control loop. It is no longer
a cross-repo contract, so it does not need the versioning ceremony that went with that.

---

## What a merge would have fixed, and what we do instead

The investigation found six couplings. Four are infrastructure ownership and are working as
intended — this repo owns InfluxDB, Grafana, `alphaess-net`, the volumes and all four scoped
tokens; `battery-planning` declares them `external` and defines none (`DEPLOY.md:928`). Those
stay.

Three are real defects. Two get fixed; one is better left alone.

### Part 1 — Revise `DESIGN-dispatch.md` (do this now)

Prerequisite for the dispatcher build, not deferred.

- **§2** — rewrite for both halves in `dispatch/`. Keep the component diagram, drop the repo
  boundary and the bind-mount discussion.
- **§6.1** — monitors 2–4 (`plan-in-influx`, `slots-written`, `slots-fresh`) are pinged from
  this repo now, using `send_heartbeat()` at `collector/collector.py:407`. **Monitor 1
  (`plan-run`) still belongs in `battery-planning`** — see Part 3. Say so explicitly.
- **§9 open question 3** — the translator can no longer import `Marstek-planning.py`'s
  `maxChargeSpeed`/`maxDischargeSpeed`. They must come from the plan or from this repo's own
  config. Restate.
- **§10** — the dispatcher is a service in *this* compose file, not a foreign project joining
  the network.
- Record the merge decision and the licence reasoning so it is not re-litigated.

### Part 2 — Fix the two seam defects

#### 2a. Publish battery capacity into InfluxDB — **done 2026-08-17**

`27900` is written in six places across both repos. Two tests guard it —
`battery-planning/tests/test_hardware.py` and `tests/test_grafana_provisioning.py:290` — and
**neither crosses the boundary**. 27,900 is the commandable capacity and no change to it is
scheduled — the ~30,500 Wh in `battery-planning/hardware.py:8` is unmodelled headroom, not a
pending upgrade — but any change that did happen would touch both repos at once, and nothing
detects a half-done one. The dashboards divide the planner's `soc_wh`
by this repo's copy of the number, so a mismatch renders as a plausible-looking wrong
percentage rather than as an error.

Make the number travel with the data that depends on it:

- **`battery-planning`**: add `capacity_wh` as a field on each `plan` point in
  `Marstek-planning.py:writePlanToInflux` (~line 2165), from `hardware.CAPACITY_WH`. One
  field; no break for existing readers.
- **here**: change the `capacity_wh` dashboard variable in `grafana/generate-battery-plan.py`
  and `generate-battery-score.py` from a constant to a Flux query reading that field. Removes
  three of the six copies and makes the rest live.
- Extend `tests/test_grafana_provisioning.py:290` to cover `alphaess-battery-score.json`,
  which it currently misses despite that file carrying `27900` at line 1644.

`.env`'s `BATTERY_CAPACITY_KWH` stays — `pricing.py` needs it independently of any plan.

**As shipped**, three things the plan above did not anticipate. All three are written up at
length above `CAPACITY_QUERY` in `generate-battery-plan.py`, which is the canonical copy:

- `alphaess-dashboard.json` carried a fourth copy of the literal and has **no generator**, so
  it is hand-maintained. It is also the dashboard the dispatcher will be watched on.
- "The newest plan" is not the last row: `plan` is tagged with `plan_run`, and runs share a
  horizon end, so neither a bare `last()` nor a sort on `_time` picks a run. The query selects
  on parsed `plan_run`, the same way `NEWEST` does and for the same reasons.
- The test extension went further than "cover the score dashboard" — it pins every dashboard
  to one query string, and asserts `current == {}` and `refresh == 1`, which are the two ways
  a query variable silently goes back to serving a constant.

Left as future work: the score dashboard's `plan_score` measurement still carries no capacity
of its own, so a capacity change re-renders past days at the new number. Visible and all at
once, which is the improvement; still not history.

#### 2b. Make the `planning` schema break loudly

A field rename in `battery-planning` currently blanks panels here with no error anywhere.
Once the translator reads the same schema, the same rename silently stops dispatch.

- The translator validates every field it reads on each run and pings monitor #2 with
  `status=down` naming the **missing field**, following commit `78c94a9`.
- Commit `tests/fixtures/planning_schema.json` naming the measurements, tags and fields this
  repo depends on. One test asserts the translator's reads are a subset of it; another asserts
  every `from(bucket: "planning")` query in the dashboard JSON only references listed fields.
  That turns an invisible cross-repo dependency into a reviewable file.

#### 2c. Not fixing: the duplicated EnergyZero fetch

`collector/prices.py:153` and `battery-planning/Marstek-planning.py:1385` call the same
endpoint with the same params, and both are maintained. The only real dedupe would be for the
planner to read `market_price` from the `alphaess` bucket instead of fetching — making every
planning run depend on this repo's price pipeline, and dropping the planner's on-disk price
cache and its ENTSOE-primary path. That trades 40 duplicated lines for a new runtime coupling
in the wrong direction. Deliberately left as is.

### Part 3 — One change in `battery-planning` (deferred)

Add a Kuma heartbeat to `plan-now.sh` for monitor #1 (`plan-run`). That repo has **zero**
heartbeat or Kuma references today; `plan-now.sh` exits 1 on failure and nothing watches it.
Once dispatch depends on fresh plans, a silent planning failure degrades to "no dispatch at
all" within four hours.

Its own PR in that repo. Port the shape of `send_heartbeat` from `collector/collector.py:407`
— in particular, rebuild the query string rather than using `params=`, which is the reason
that function looks the way it does.

---

## Also settled while investigating

- **Historical plans are review and fuzz input only, never committed.**
  `plan_YYYYMMDD_HH.txt` carries a load-forecast column — occupancy data at 15-minute
  resolution — and both repos are public. Same reasoning
  `battery-planning/tests/test_golden_plan.py` already applies by using invented load/PV
  numbers. They land under `../battery-planning/tests/testdata/plans/`, covered by that
  repo's `.gitignore:28`.
- **`0x0885` is the dispatch mode register**, not `0x0883` (which is reactive power).
  `DESIGN-dispatch.md` §7 had this wrong in two places; corrected 2026-08-15.
- **`POLL_INTERVAL_SECONDS` in `battery-planning` is a guess about this repo's runtime
  config** (`battery-planning/influx_source.py:144`), used for a coverage gate that fails
  silently if the two drift. Known, not yet addressed.
