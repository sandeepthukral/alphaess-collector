# Dispatch control — design

Turning the `battery-planning` optimiser's output into Modbus commands that make the
AlphaESS SMILE-G3-S5 actually follow the plan.

Both halves live in this repo, under `dispatch/`. The optimiser stays in `battery-planning`;
InfluxDB is the only thing that crosses between them. See `PLAN-repo-seams.md` for why that
boundary is where it is, and what still needs fixing across it.

Status, 2026-08-16: **built and running in dry run; not live.** The whole path works end to
end — `translate.py` reads the plan from InfluxDB, writes `slots.json`, and `scheduler.py`
connects to the inverter, reads SoC and limits, decides, and releases on exit. Verified from
inside the container image on 2026-08-16, against the real inverter and the real `planning`
bucket: 46 slots, and the direction guard correctly refused a discharge to 10.0 % against a
live SoC of 10.4 %.

What has NOT happened: no `--live` write has ever been made by this code, the AlphaESS app's
own price control has not been turned off (§8), and the service is not deployed on the NAS.
The compose service ships without `--live` on purpose; see §10.

What exists in this repo:

- `dispatch/` — `registers.py`, `plan.py`, `translator.py`, `translate.py`, `slots.py`,
  `state.py`, `scheduler.py`, plus a Dockerfile and an entrypoint that runs the translator on
  an hourly timer beside the 60 s loop.
- Tests: goldens over three synthetic plans and nine human-reviewed real runs, invariants over
  the whole 138-run archive, table-driven boundary tests for `decide()`, a pymodbus simulator
  smoke test, and deployment-shape tests over the compose file and Dockerfile.
- `dispatch/test_mode1_negative.py` — a one-shot hardware experiment for §9's first open
  question. Runs; aborted correctly on its first dry run. Still awaiting its conditions.

Where the evidence in this document comes from:
- `~/Downloads/dispatch_experiment.py` — the phase runner behind the 16:11–16:20 evidence
  quoted throughout this document. Verified against real hardware.
- `~/Downloads/battery_scheduler.py` — the handover's file-based translator. **Superseded.**
  It imports `advise`/`hardware` from the planner checkout via `sys.path`, merges intervals
  into blocks, and scopes slots as `date` + local `HH:MM`. §2, §4 step 6 and §4.3 reject all
  three. Useful only as a reference for `advise.classify`'s action vocabulary.
- `~/Downloads/alphaess_dispatch_scheduler.py` (512 lines) — the handover's dispatcher.
  Verified against real hardware for modes 2 and 3. **Its register layer independently
  confirms every address and encoding in `dispatch/registers.py`**, including `0x0885` for
  mode, and it contributes two registers this document had not recorded: `0x012C`
  `battery_max_charge_power` and `0x012D` `battery_max_discharge_power`.

  Reusable as-is: the write ordering (mode, power, SoC, duration, then `start=1` **last**, so
  a partially written command is never live), the live max-power clamp, the release-on-every-
  exit-path guard, and the local heartbeat file — which `--alive` checks without opening a
  Modbus connection, for the same reason §6.2 forbids a Kuma port monitor.

  Not reusable: its slot model is `date` + local `HH:MM` with weekday recurrence, which §4.3
  replaces with UTC instants; its own docstring concedes that date-scoped slots cannot wrap
  past midnight, which is the same defect seen from the other side.

Sources: the Modbus handover doc (2026-08-12, updated through 2026-08-15), the live
`dispatch_experiment.py` run of 2026-08-15 16:11–16:20, and the two repos themselves.

---

## 1. The constraint everything follows from

**The inverter cannot store a future schedule.** The dispatch register block holds exactly
one live command — one mode, one power, one SoC target, one duration countdown. Writing a
new command overwrites the only one that exists.

So there is no "register today's plan with the battery". Something must run continuously,
re-issuing the right command as time passes. The duration field (`Para 6`) is a dead man's
switch: if it expires unrefreshed, the inverter reverts on its own. That self-revert is
verified — three times in the 2026-08-15 run alone — and it is the fail-safe the whole
design rests on.

Two consequences that shape everything below:

- **Exactly one process may hold the Modbus connection.** The inverter refuses a second
  (`Errno 61`). This is not a code convention, it is a hardware limit, and it rules out
  whole categories of monitoring (§6).
- **Silence is the safe state.** When in doubt, stop writing and let the command expire.
  This is only true once the AlphaESS app's own price-based control is off — see §8.

---

## 2. Components and where they live

```
Marstek-planning.py            battery-planning, every 3h via DSM Task Scheduler
        |  writes measurement `plan` to InfluxDB bucket `planning`
        |    one point per 15-min interval, tagged plan_run
        v
    ---- repo boundary: InfluxDB is the only thing that crosses it ----
        v
dispatch/translate.py          alphaess-collector, hourly timer in the same container
        |  reads the bucket, calls translator.py, writes slots.json atomically
        v
dispatch/scheduler.py          alphaess-collector, continuous 60s loop
        |  picks the slot for "now", writes the dispatch registers
        v
    inverter @ 192.168.68.151:502
```

**Both halves live in `alphaess-collector`, in `dispatch/`.** One image, one compose service,
one PR per change.

An earlier draft of this section put the translator in `battery-planning`, on the grounds
that `advise.py` already converts Wh-per-interval into a W setpoint and `hardware.py` already
holds capacity, so building it here meant reimplementing them. **That argument died with the
decision to read the plan from InfluxDB rather than from the plan file.** A translator reading
Influx needs none of that repo's code — only its schema, which this repo's Grafana already
consumes across 25 queries in `grafana/alphaess-battery-plan.json`. What is left to
reimplement is roughly fifty lines of unit conversion, which would otherwise arrive in
camelCase into a ruff-linted snake_case codebase, and whose tests port over cleanly.

The repo boundary itself stays, for a reason that has nothing to do with this feature:
`battery-planning` is a GitHub fork of an **unlicensed** upstream, so the fork relationship is
the attribution chain for 2,450 lines of `Marstek-planning.py`. See `PLAN-repo-seams.md`.

Everything operational this feature needs already exists here: a long-running container with a
restart policy, `send_heartbeat()` at `collector/collector.py:407`, a Grafana health
dashboard, a DEPLOY.md runbook, and scoped InfluxDB tokens. The plan-vs-actual reconciliation
(§5.4) is the same shape as `efficiency.py` and `pricing.py` and reads the same database.

The collector process itself stays read-only against the battery. The dispatcher is a separate
directory, image and compose service — the single-connection limit (§1) makes that mandatory,
not stylistic.

**`slots.json` remains the seam between the two halves, but it is now an internal artefact**
rather than a cross-repo contract. It still earns its place: it decouples an hourly batch job
from a 60 s control loop, survives a restart of either, and can be read by a human at 03:00
when the battery is doing something surprising. What it no longer needs is the versioning
ceremony a cross-repo file format would have required — no bind-mount across compose projects,
no schema negotiation with another repo.

Keep it a file rather than routing slots through InfluxDB. A control loop that re-reads a
local file survives an Influx outage, which is the right failure mode: the planner going
quiet should degrade dispatch gracefully, not stop it mid-slot.

**The one thing that crosses the boundary is the `plan` schema.** That makes it a dependency
nothing currently tests — a field rename in `battery-planning` would blank Grafana panels
today and silently stop dispatch tomorrow. `PLAN-repo-seams.md` §2b covers the fix: a
committed schema fixture plus a runtime check that fails loudly on monitor #2.

---

## 3. Reading the plan

### 3.1 What the planner stores

`Marstek-planning.py:writePlanToInflux` writes measurement `plan` to bucket `planning`,
one point per 15-minute interval, **timestamped at interval start in UTC**, tagged
`plan_run` (the run's UTC ISO8601 timestamp, `Marstek-planning.py:2404`).

Fields used here: `soc_wh`, `charge_wh`, `discharge_wh`, `import_wh`, `export_wh`,
`price_buy`, `price_sell`, `reserve_wh`.

`influx_source.planPoints(start, stop)` already reads this and returns the `plan_run` tag
with each point.

### 3.2 `soc_wh` is END-of-interval

Settled from the LP constraint at `Marstek-planning.py:2006-2008`:

```
t == 0:  soc[t] == initialCharge + pvDirect[t] + Effcharge*charge[t] - discharge[t]/Effdischarge
t >  0:  soc[t] == soc[t-1]     + pvDirect[t] + Effcharge*charge[t] - discharge[t]/Effdischarge
```

`soc_wh` on a point includes that interval's own charge and discharge, so it is the SoC the
plan expects **at the end** of the interval. `initialCharge` is the SoC at the start of
interval 0, read live from InfluxDB — the planner refuses to run without it.

The dispatch target for an interval is therefore that interval's **own** `soc_wh`, not the
next point's. As a percentage: `soc_wh / 27900 * 100` (`hardware.py:CAPACITY_WH`).

Two facts that fall out and matter:

- `pvDirect` is always 0 here — `IDX_PV_DIRECT=4  # forecast/actual Wh from DC-coupled PV
  (always 0 here - no such group)`, correct for an AC-coupled site. The recurrence has no
  hidden PV term.
- **`charge_wh`/`discharge_wh` are AC-side; `soc_wh` is battery-side.** The efficiency
  factors sit between them. This is convenient: the AC-side number is what you command as a
  power setpoint, the battery-side number is what you write to the SoC register. Do not
  try to derive one from the other — read both from the point.

### 3.3 Always the latest plan

For each interval, use **the newest `plan_run` whose horizon contains that interval**. This
generalises `report_day.py:inForcePlans()`, which picks the most recent run at or before an
interval. Looking forward, all runs are "before", so it reduces to newest-wins — but the
containment test still matters, because a newer run with a shorter horizon should fall back
to an older run for the tail rather than leave it unplanned.

The translator re-reads the bucket from scratch on every run and rewrites `slots.json`
whole. There is no incremental state to go stale.

---

## 4. Part A — the translator (`dispatch/translator.py`, run by `dispatch/translate.py`)

The pure half is `translator.py`; `translate.py` is the job that wraps it — one Influx read,
one atomic file write, two Kuma pings (§6.1). Not a daemon, and **not** appended to
`plan-now.sh` — that script is in the other repo, and the whole point of reading from Influx
is that this half no longer has to run there.

**It runs hourly, not 3-hourly, and there is no offset.** An earlier draft ran it on the
planner's own cadence, offset ~10 minutes after the DSM schedule so it would read a fresh
plan. That coupling is not worth its cost: a fixed 3-hourly loop inside a container that
restarts whenever it likes has no fixed phase relative to DSM, so the offset it was tuned for
survives exactly until the first restart, after which the translator can sit a full cycle
behind a plan that already landed. Running four times as often costs one Influx query an hour
and removes the schedule dependency entirely.

If the planner is late, the translator re-reads the previous plan and the freshness gate (§5)
decides whether it is still usable. That is the intended degradation, and it is now the only
mechanism — nothing depends on the two schedules lining up.

```
1. READ plan points from Influx: bucket `planning`, measurement `plan`,
   from now-rounded-down-to-quarter to +36h.

2. FOR each interval: pick the governing plan_run (§3.3).

3. FOR each interval: classify into an action (§4.1).

4. CONVERT energy to power:  power_w = wh * (60 / interval_minutes)   # x4 at 15 min
   CLAMP to the inverter's max charge/discharge (§9, open).

5. TARGET SoC = this interval's own soc_wh as a percentage (§3.2).
   GUARD the direction rule: charge needs target above current SoC,
   discharge needs target below it. If the plan's own trajectory violates
   it, downgrade the slot to `hold` rather than emit a silent no-op.

6. EMIT one slot per interval. Merge two adjacent slots ONLY if action AND
   power AND target are identical.

7. WRITE slots.json atomically (temp file + rename), with a header carrying
   generated_at, plan_run, horizon_end, interval_minutes.

8. PING the Kuma monitors (§6): slots-written, and plan-in-influx.
```

**Step 6 deliberately departs from `advise.py`.** That module collapses intervals into
blocks because its reader is a human who does not want 104 rows. The dispatcher is a
machine that rewrites the register every 60 s regardless, so merging buys nothing
operationally and costs the plan's per-interval power shaping. Keep the granularity; let
the file be long.

**Step 3 needs a real floor.** `advise.py` treats anything above 1 Wh as action. For
dispatch use ~50 Wh, so a rounding artefact cannot create a slot. (Handover open item 4
makes the same point about the PV day/night threshold.)

### 4.1 Action mapping

The plan is an open-loop schedule and Mode 2 is open-loop control. Where the plan is
*specific* about wattage — sell 4 kW into the evening peak, buy at the 03:00 trough — forced
dispatch is right. Where the plan is *indifferent* to the exact wattage — cover the house
load, absorb whatever surplus exists — a closed-loop mode tracks reality and absorbs forecast
error for free.

`advise.py:classify()` already draws this distinction, calling out "sell" (discharging to
grid) versus "cover load" (discharging into the house), because they earn different prices.
It maps onto dispatch:

| Plan interval | Meaning | Command |
|---|---|---|
| `discharge_wh > floor`, `export_wh > floor` | sell to grid | Mode 2, power `+w`, target SoC |
| `charge_wh > floor`, `import_wh > floor` | buy to charge | Mode 2, power `-w`, target SoC |
| `discharge_wh > floor`, `export_wh ≈ 0` | cover house load | **release dispatch** (`start=0`) |
| `charge_wh > floor`, `import_wh ≈ 0` | absorb PV surplus | **release dispatch** (`start=0`) |
| all ≈ 0, `soc_wh` at capacity, `export_wh > 0` | gauge saturated, solar still coming | **release dispatch** (`start=0`) — see below |
| all ≈ 0, `soc_wh` at capacity, `export_wh ≈ import_wh ≈ 0`, `pv_forecast_wh > 0` | gauge saturated, PV forecast meets the house exactly | **release dispatch** (`start=0`) — see below |
| all ≈ 0 | hold / night idle | Mode 3, power 0 |

**"Absorb surplus" and "cover load" are both plain self-consumption, which is no dispatch at
all.** Every baseline in the 2026-08-15 run shows it: `start=0`, battery charging from
surplus, grid at ~0. That is exactly what the plan wants for those intervals.

The alternative — commanding forced charge at `charge_wh × 4` W on a PV-surplus interval —
pulls the shortfall from the grid whenever solar underdelivers, importing energy the plan
explicitly priced at zero. That is the normal case for a forecast, not an edge case.

**Consequence worth naming: `start=0` is now a deliberate commanded state, and at the
register level it is indistinguishable from "the dispatcher has crashed."** Only
`dispatch_start` says whether dispatch is active, and it reads 0 in both cases. This is why
the heartbeat file and the Kuma push monitors in §6 are load-bearing rather than a
convenience — they are the only things that can tell those two apart.

#### 4.1.1 Harvesting above the gauge

The fifth row is not in `advise.py` and was added on 2026-08-15 after measuring the battery
rather than the plan.

**The SoC gauge saturates before the battery does.** Over 25 days of collector data
(2026-07-22 → 2026-08-15), once `soc_percent` reads 100 the battery goes on absorbing a mean
of **1,375 Wh/day**, max 2,240 Wh, on 16 of 25 days. It is real stored energy, not a
reporting artefact: 18,615 Wh of the 22,005 Wh absorbed came back out again before the gauge
left 100 %, which is 85 % — ordinary round-trip loss. And all of it is solar. Grid-sourced
charge across those 1,075 minutes was **0 Wh**.

**That headroom cannot be reached by command.** `target_soc_pct` (`0x0886`) is a percentage
*of the gauge*, so once the gauge reads 100 a Mode 2 command asking for 100 has nothing left
to ask for — the inverter believes it has arrived. The only control state that keeps
absorbing past that point is self-consumption, where there is no target and the BMS takes PV
until it physically stops. On these intervals `self` is not marginally better than `hold`; it
is the only mechanism that reaches the energy.

Three constraints keep the rule narrow:

- **Only at the plan's own capacity.** Below it, the LP had room and surplus and still chose
  not to charge — a decision (a cheaper trough later, or the export being worth more), and
  overriding it would be second-guessing the optimiser inside its own model. At capacity the
  LP is not choosing, it has run out of the variable.
- **Only while net-exporting.** `self` discharges as well as charges. On 2026-08-05 the same
  `hold` run flips from exporting to importing 60 Wh at 18:00, three intervals before a
  1,175 Wh/interval dump into the evening peak; releasing there would spend exactly the
  inventory the dump depends on. The rule is therefore evaluated **per interval, before
  merging**, and that run splits into `self` 16:45–18:00 and `hold` 18:00–18:45.
- **No calendar.** The condition is read from the plan and the season falls out of it: across
  the 137-run archive it fires in the 16:00 and 17:00 local hours only, on 13 of 16 days.

Its own threshold, `SURPLUS_FLOOR_WH = 10`, deliberately *not* `ENERGY_FLOOR_WH`. That floor
asks "did the LP intend a dispatch"; this asks "is any solar going to the meter". Reusing
50 Wh drops three of the four harvestable intervals on 2026-08-05, whose exports sit at
exactly 50 Wh.

Measured effect on the archive: all changes are `hold → self`, none in the other
direction. Deduplicated to the newest run per interval — what would actually have been
dispatched — **57 intervals across 15 of 16 days**, confined to the 16:00 and 17:00 local
hours (47 of them from the surplus branch, 10 more from the balance point).

**The balance point is included, and it is the larger half of the win.** `export_wh` is a
*difference of two forecasts*, so where they cancel its sign is set by the error rather than
the signal — and that is exactly the late-afternoon window this rule is for. Pairing every
plan interval against actual PV over the same 15-minute window:

| local hour | forecast | actual | median error | actual > forecast |
|---|---|---|---|---|
| 16:00 | 334 | 630 | +269 | 83 % |
| 17:00 | 194 | 449 | +264 | 88 % |
| 18:00 | 107 | 289 | +183 | 91 % |

The load forecast is fine (187 planned against 194 actual); it is PV that runs ~2.3× low.
Actual net at 17:00 is a median **+285 Wh** with only 17 % of intervals in deficit. So a
plan-balanced interval at full is a coin flip called by the least trustworthy digit in the
plan, and in the plan's own model `hold` and `self` are identical there anyway — the battery
neither charges nor discharges under either.

**`pv_forecast_wh > 0` is what makes this safe year-round rather than a summer rule.** In a
balanced interval export and import are both zero, so `pv_forecast_wh` *equals the household
load by identity*. The branch therefore fires only where the plan says solar is covering the
whole house — a "the sun is strong right now" test, which self-gates in winter and cannot
fire at night. Across the archive those intervals carry 158–241 Wh per quarter hour
(630–960 W).

**Forecast deficits are still frozen, whatever the sun is doing.** 18:00–18:45 on 2026-08-05
is actually in surplus 65 % of the time, so releasing there has positive expected value — but
it is the one place where being wrong costs something specific: the p10 case drains ~175 Wh
per interval out of the inventory the 18:45 peak dump depends on. A certain small loss traded
for an uncertain small gain, immediately before the trade the day is built around. The real
fix for that window is the PV forecast in `battery-planning`, not a hedge here.

**Not a capacity correction.** The obvious reading is that `CAPACITY_WH = 27,900` is simply
too low. Raising it would make the LP plan SoC targets the gauge cannot report and the
dispatcher cannot command — unreachable by construction. 27,900 stays as the *commandable*
capacity and this headroom stays unmodelled, harvested opportunistically — the extra is only
reached after a long hold at 100 % or a large surplus, which is exactly the condition under
which harvesting it costs nothing. What the ~30,500 Wh figure in `battery-planning/hardware.py`
is *for* is a separate question and should not be conflated with this.

### 4.2 Why not Mode 1 for surplus

Mode 1's official description is *"battery not allowed to discharge; after PV supplies the
load, the excess charges the battery"* — which would be ideal for the surplus case. It does
not do that on this firmware.

Verified 2026-08-15 under genuine surplus, using `solar = house + battery_charge + export`:

| Phase | Sample | Solar | Battery | Grid | ⇒ House | Surplus went to |
|---|---|---|---|---|---|---|
| mode1 baseline | 16:18:00 | 832 | −331 (charging) | −1 | 500 W | battery |
| mode1 during | 16:18:45 | 797 | 0 | −331 (export) | 466 W | **grid** |
| freeze baseline | 16:14:58 | 919 | −361 (charging) | 0 | 558 W | battery |
| freeze during | 16:15:43 | 897 | 0 | −366 (export) | 531 W | **grid** |

Solar (~800–920 W) genuinely exceeded house load (~470–560 W), and the surplus was
demonstrably charging the battery in both baselines. Mode 1 stopped it and exported instead.

The control is airtight: at SoC 90.0 % the battery was charging from surplus immediately
before Mode 1, refused during it, and resumed immediately after (−240 W at 16:20:00). "Too
full to accept charge" is ruled out by the run's own bracketing samples.

**Mode 1 and Mode 3 are empirically identical here — both freeze at 0 W and export the
surplus.** Handover §8 / Test B is closed.

One caveat before writing Mode 1 off permanently: the official table's Mode 1 entry says
*"Battery not allowed to discharge (**Pdispatch < 32000**)"*. The test sent `power=0 W`, raw
`32000` exactly — not `< 32000`. Mode 1 may need a negative (charge-direction) power to mean
"do not discharge, and charge up to this much from excess". See §9.

**That caveat was right, and this section's headline is only true of `Pdispatch = 32000`.**
Measured 2026-08-16 (§9.1): given a genuinely negative power, Mode 1 charges at the commanded
setpoint and exports the remaining surplus. It does not freeze. The conclusion *"not Mode 1
for surplus"* survives anyway, for a different reason — a setpoint that is obeyed is a charge
command, not self-consumption — but do not cite the freeze as the reason.

### 4.3 `slots.json` contract

As handover §7, with one change: **`start`/`end` are UTC instants, not `date` + local
`HH:MM`.** The planner's own code comments on why — the local time string repeats itself on
the October DST night. A local-clock schedule has a genuinely ambiguous hour once a year, on
a control path. Store UTC; render local for humans.

```json
{
  "generated_at": "2026-08-15T14:05:03Z",
  "plan_run": "2026-08-15T14:04:31Z",
  "horizon_end": "2026-08-16T13:00:00Z",
  "interval_minutes": 15,
  "slots": [
    { "start": "2026-08-16T00:00:00Z", "end": "2026-08-16T03:00:00Z",
      "action": "charge", "power_w": 4000, "target_soc": 90 },
    { "start": "2026-08-16T03:00:00Z", "end": "2026-08-16T05:00:00Z",
      "action": "self" },
    { "start": "2026-08-16T21:00:00Z", "end": "2026-08-16T22:00:00Z",
      "action": "hold" }
  ]
}
```

`action` is one of `charge`, `discharge`, `self`, `hold`. `power_w` and `target_soc` are
required for `charge`/`discharge` and absent otherwise. Overlapping slots are a warning, not
an error; the earliest match wins.

---

## 5. Part B — the dispatcher, 60 s loop

```
EVERY 60 seconds:

1. IF slots.json mtime changed: reload and validate.

2. FRESHNESS GATE:
      if generated_at older than ~4h, or horizon_end already passed
      -> issue NO command, ping slots-fresh DOWN with the cause.
   The dead man's switch expires and the inverter falls back to
   self-consumption. Never extrapolate a stale plan.

3. FIND the slot covering now. None -> no command (same fallback).

4. READ live SoC (register 258, raw/10) and re-check the direction rule
   against LIVE SoC, not planned SoC. Violation -> treat as hold.

5. DETECT HIJACK: if the block reads start=1 with a mode/power/soc this
   process did not write, the app (or something else) is dispatching.
   Log it, ping inverter-not-hijacked DOWN, and continue — our 60s
   rewrite wins, but the operator needs to know.

6. BUILD the command:
      charge     -> mode 2, power = -power_w, target_soc
      discharge  -> mode 2, power = +power_w, target_soc
      hold       -> mode 3, power 0, no SoC written
      self       -> release dispatch (start=0)
   Encode: raw_power = 32000 + watts ; raw_soc = pct / 0.4

7. WRITE the dispatch block, duration = 300 s (5x the refresh interval).

8. READ BACK, whenever anything was written -- including the single release
   on the way into idle, which is a write like any other.

9. PING monitors #4-#8 from the completed tick. Append to
   dispatch_audit.log: slot, command, readback, live SoC, live power.

10. ON error: log, do NOT exit, retry next tick. Never open a second
    Modbus connection.
```

The same command is rewritten ~15 times per 15-minute slot. That is intentional and
idempotent: the repetition *is* the dead man's switch refresh, and Mode 2 stops on its own
when the SoC target is reached.

### 5.1 Timing facts the loop must respect

- **~15 s lag** between writing a command and the battery responding. Reproduced in all
  three phases of the 2026-08-15 run: the first sample after each write still shows the old
  battery value while the registers already read back correctly. Never judge a command from
  one early sample.
- **Revert is not atomic, and the ordering is not stable.** 2026-08-14 saw `start=1 mode=0
  dpwr=0` with the battery still discharging at 4194 W; 2026-08-15 at 16:16:43 saw the
  opposite ordering, `start=1 mode=0` with start outlasting mode. Never conclude from a
  single readback during a transition.
- **`dispatch_time` is not a smooth countdown** — observed reading `300s` three times across
  two minutes, then straight to expiry. Do not poll it to predict the revert moment. It
  reads `90` when idle; ignore it entirely unless `start=1`.
- **Delivered power differs from commanded, asymmetrically.** Discharge: commanded 4500 W,
  delivered 4173–4215 W (≈295 W under), reproduced across two runs. Charge: commanded
  1500 W, delivered ~1551 W (≈50 W over). Consistent enough to be a calibration factor
  rather than noise. Do not assume exact wattage compliance when planning energy volumes.

### 5.2 Encodings

```
raw_power = 32000 + watts        # >0 discharge/export, <0 charge/import
raw_soc   = target_pct / 0.4     # i.e. x2.5 — the "0.392 %/bit" community claim is wrong
```

Measurement registers use different scaling from setpoint registers: SoC register 258
(`0x0102`) is `raw / 10`. Solar must be read from the **PV meter** at register 161
(`0x00A1`) — the DC MPPT registers `pv1_*`…`pv4_*` always read 0 on this AC-coupled site.
That is correct, not a bug to fix.

Capacity cross-checks out: ~4200 W for ~75 s ≈ 88 Wh, which against 27,900 Wh is 0.31 %, and
the observed SoC moved 90.4 → 90.0. The power register, the SoC register and
`hardware.py:CAPACITY_WH` all agree.

### 5.3 Sign conventions

`battery_power` (register 294): **negative = charging, positive = discharging.** Verified by
commanding a discharge and watching it go from −423 W to +4205 W (2026-08-15 16:12).

### 5.4 Reconciliation (phase 2)

A daily job in this repo, alongside `efficiency.py` and `pricing.py`, comparing
`dispatch_audit.log` against measured actuals in InfluxDB: for each slot, did the battery
deliver the planned energy within tolerance? This is the only check that catches "every
monitor green but the battery is not following the plan", and it feeds the `dispatch-vs-plan`
monitor in §6.

---

## 6. Monitoring

Uptime Kuma, following this repo's existing pattern: `send_heartbeat(url, status, msg)` in
`collector.py:407`, configured through `*_HEARTBEAT_URL` env vars.

Three principles:

1. **Every monitor must catch something no other monitor catches.** Otherwise one outage
   fires five alerts and you learn nothing from any of them.
2. **Push, not HTTP.** Kuma cannot ask "is the plan fresh?" or "did the readback match?".
   The process reports its own semantic state.
3. **`status=down` with a cause, not silence.** This repo already learned this — commit
   78c94a9, "Carry the failure cause in the recovery heartbeat". An outage that only reads
   "no ping received" is the difference between diagnosing from a phone and opening
   container logs.

### 6.1 The monitors

| # | Monitor | Pinged by | Interval | Catches, uniquely |
|---|---|---|---|---|
| 1 | `plan-run` | `plan-now.sh` **(battery-planning)** | 3 h + grace | The optimiser failed or DSM never fired it |
| 2 | `plan-in-influx` | translator | 2 h | Plan made, but the Influx write silently failed |
| 3 | `slots-written` | translator | 2 h | Plan readable, but translation failed |
| 4 | `slots-fresh` | dispatcher | 15 min | Translator died while the dispatcher stayed healthy |
| 5 | `dispatcher-alive` | dispatcher | 2 min | The control loop is dead — nothing is dispatching |
| 6 | `dispatch-confirmed` | dispatcher | 5 min | Loop alive but the inverter is rejecting writes |
| 7 | `inverter-not-hijacked` | dispatcher | 5 min | The app re-asserted control over the same registers |
| 8 | `soc-floor` | dispatcher | 15 min | SoC below `minBatterySOCPct` — safety backstop |
| 9 | `dispatch-vs-plan` | daily job (§5.4) | 24 h + grace | All green, but the battery is not following the plan |

**Monitor 1 is the only one that lands in `battery-planning`, and it is the only change this
feature makes to that repo.** That repo has no heartbeat and no Kuma reference anywhere today;
`plan-now.sh` exits 1 on failure and nothing watches it. It is a prerequisite, not a
nice-to-have — the freshness gate means a silent planning failure degrades to "no dispatch at
all" within four hours. Tracked as `PLAN-repo-seams.md` Part 3, its own PR over there. Port
the shape of `send_heartbeat()` from `collector/collector.py:407`, including the rebuilt query
string, which is the reason that function looks the way it does.

Monitors 2–4 are pinged from this repo and can call `send_heartbeat()` directly.

Monitor 2 is separate from 1 on purpose. `writePlanToInflux` is *deliberately non-fatal*: by
the time it runs, the plan is printed and on disk, and its own comment says a missing
dashboard point is a smaller loss than a planning run that dies at the last step. That
reasoning was correct when Influx held only a dashboard copy. Now it is the control path's
only input, so the failure must be visible even though the plan run itself "succeeded".

Three of #4-#8 depart from the obvious rule, and each departure exists to stop a monitor
crying wolf -- which is the failure mode that ends with all of them ignored:

- **#6 `dispatch-confirmed` is UP when there was nothing to confirm.** A release or an idle
  tick writes no command, so a readback proving nothing is not evidence of a rejected write.
  It is also unconditionally up in dry run, where nothing is written and every readback
  therefore mismatches — left unguarded it would sit red for the whole observation phase.
- **#8 `soc-floor` is not pinged at all when the SoC register could not be read.** A `down`
  would assert the battery is below its floor, which is not what was observed. Its 15-minute
  window still turns a *persistent* read failure into a down without a single dropped read
  doing so.
- **#4 `slots-fresh` carries the staleness reason verbatim**, because "the translator died"
  and "the plan ran out" have different fixes and that string is the only thing separating
  them on a phone.

Monitors 4 and 5 look similar and are not. #5 down means the process is gone. #4 down means
the process is fine and is deliberately refusing to dispatch because its input went stale.
Different causes, different fixes, and #4 will be the common one.

### 6.2 What must NOT be monitored

**Do not create a Kuma TCP Port monitor against 192.168.68.151:502.** It is the obvious
thing to add and it would break the system: the inverter allows exactly one Modbus TCP
connection, so the monitor would either fail permanently or steal the connection from the
dispatcher. Every inverter-facing check must be self-reported by the dispatcher — which is
also why `--status` cannot run while the scheduler is up.

### 6.3 Severity

The fail-safe is silence, so most failures cost money rather than doing damage. Tier
accordingly:

- **Wake-up:** #7 `inverter-not-hijacked`. A competing controller can force-charge at 5 kW
  from the grid at any price — the 2026-08-15 run caught exactly that
  (`dpwr=−5000W dsoc=100.0% dt=5580s`, grid +4596 W importing). That is real money per hour.
- **Prompt but not nocturnal:** #5, #6. No dispatch means self-consumption — a lost
  arbitrage cycle, not a hazard.
- **Daily digest:** #1, #2, #3, #4, #9. The plan cadence is 3 h and the horizon runs ~36 h,
  so one missed run is tolerable.
- **#8** is a backstop that should never fire. If it does, treat it as a bug in the
  translator's direction guard, not as an operational alert.

---

## 7. Making the command legible — the Battery Plan dashboard

Kuma answers "is it broken". It cannot answer "what is the battery being told to do right
now, and why". That question gets asked from a phone, standing in the kitchen, watching the
meter — and today the only way to answer it is to run `--status` against the inverter, which
§6.2 already rules out while the scheduler is up.

So the dashboard has to answer it. `grafana/alphaess-battery-plan.json` already shows the
plan and the battery's actual behaviour side by side; the missing middle term is **the
command** — the thing that is supposed to turn one into the other.

### 7.1 The dispatcher publishes its readback to Influx

Grafana does not speak Modbus, and must not: one connection, already spoken for. The
dispatcher is the only process that may read those registers, so it is the only process that
can publish them.

Every loop, after the write-then-verify readback it already performs for monitor #6, the
dispatcher writes one point to measurement **`dispatch_state`** in the `alphaess` bucket.
No extra Modbus traffic — this is publishing a read it already did.

**Decode at write time, not in Flux.** Store both:

| Field | Type | From | Written | Example |
|---|---|---|---|---|
| `dispatch_active` | int | `0x0880`, verbatim | every tick | `1` |
| `mode` | int | `0x0885` | every tick | `2` |
| `mode_name` | string | decoded | every tick | `"SoC control"` |
| `action` | string | decoded | every tick | `"charging from grid"` |
| `setpoint_w` | int, signed | `raw − 32000`, **sign-flipped** | every tick | `−2500` → `+2500` |
| `target_soc_pct` | float | `raw × 0.4` | every tick | `78.0` |
| `duration_s` | int | `0x0887/8` | every tick | `900` |
| `raw_0880`…`raw_0888` | int | verbatim | every tick | — |
| `expires_at` | int, unix s | write time + duration | **only while active** | — |
| `slot_start` | int, unix s | the slot being served | **only when serving one** | — |
| `slot_action` | string | the slot being served | **only when serving one** | `"discharge"` |
| `plan_run` | string | the plan that slot came from | **only when known** | `"2026-08-15T15:00:00Z"` |

Four things about that table are load-bearing:

- **The `Written` column is a contract with every query, not documentation.** The last four
  fields stop being written the moment there is nothing to say, and §7.3's third guard is
  entirely about what that does to a panel. `dispatch_active` is in the first group and is
  the raw register word rather than a bool: the dispatcher only ever writes 0 or 1, but the
  AlphaESS app writes this register too, and a readback of anything else is precisely the
  case worth seeing. Test it as `!= 0`, never `== 1`.

- **`setpoint_w` is flipped into the dashboard's convention.** The register counts discharge
  positive; every other panel on this dashboard counts charging positive — panel 9 already
  negates `battery_power_w` in Flux for exactly this reason, and its description says so. A
  "Commanded" stat that disagreed in sign with the "Battery Power now" stat sitting beside it
  would be worse than no panel at all. Flip once, in the dispatcher, where the encoding
  constant already lives.
- **`plan_run` and `slot_start` are what make the panel diagnostic rather than decorative.**
  Without them the dashboard shows a command; with them it shows *which plan asked for it*,
  which is the difference between "the battery is charging at 15 ct" and "the battery is
  charging at 15 ct because it is still serving a plan from six hours ago".
- **The raw block stays.** The decoded fields are for reading; the raw fields are for the
  morning after, when a decode turns out to have been wrong. Keeping both costs nothing at
  one point per minute and is the only way to re-derive history.

### 7.2 The panels

Existing top row is six stats at `w=4`, filling `y=0` exactly. The new material goes
directly beneath it, so the dashboard reads top-to-bottom as **plan → command → actual**.

**A stat row, `y=4`** — four panels, deliberately mirroring the row above:

| Stat | Unit | Renders as | Thresholds |
|---|---|---|---|
| Dispatch state | — | `Charging from grid` / `Discharging to grid` / `Holding` / `Released — following house` / **`NO DISPATCHER`** | grey when released, red when stale |
| Commanded power | `watt` | `+2.5 kW` charge, `−4.5 kW` discharge | green charge / red discharge, matching panel 9 |
| Target SoC | `percent` | `78 %` | plain text |
| Command expires in | `s` | `4 m 12 s` | red under 60 s |

Leave `decimals` unset on the watt stat — the generator's existing comment explains why
(`watt` self-scales, and pinning decimals gives either `850.00 W` or `2 kW`).

**A decode table, below it** — one row per register, columns `Register | Name | Raw |
Means`:

```
0x0880  Dispatch start     1       Active
0x0881  Active power   29500       −2500 W  (charging at 2.5 kW)
0x0885  Mode               2       SoC control
0x0886  SoC target       195       78.0 %
0x0887  Duration         900       15 min, expires 16:45:00
```

This is the literal answer to "human-readable instead of register values" — but keep the raw
column. Half the value of this table is being able to check a decode against the spec without
leaving the dashboard, and every encoding in §5.2 was got wrong by somebody first (the
community's `0.392 %/bit` claim is in the wild precisely because nobody could see both
columns at once).

**A third series on panel 5.** "Planned SoC vs actual SoC" becomes planned vs **commanded**
vs actual. That single change closes the loop visually: plan says 78 %, dispatcher commanded
78 %, battery reached 71 % — and the gap between the second and third lines is delivery error
while the gap between the first and second is a dispatcher bug. Those are different problems
and today the chart cannot distinguish them. Dashed line, distinct colour, via
`series_override` like the existing ones.

### 7.3 Staleness is the failure mode to design against

A dead dispatcher does not clear `dispatch_state`. It leaves the last point sitting there
forever, and a panel querying `range(start: -1h) |> last()` will cheerfully render a command
that expired fifty minutes ago as the current state of the battery. That is worse than a
blank panel: it is a confident wrong answer, in the one place you go to check.

Three guards, all required:

1. **Query a short window.** `range(start: -5m) |> last()` — five loop iterations. Past that
   the panel goes to No data.
2. **Map No data to a loud value.** `NO DISPATCHER` in red, via the stat panel's
   `noValue` option, not an empty cell. The existing "Plan age" stat is the precedent for
   treating age as a first-class reading rather than an absence.
3. **Treat a stale *field* as its own case.** Guards 1 and 2 assume staleness is
   all-or-nothing. It is not: the conditional fields in §7.1's table stop being written while
   everything beside them keeps flowing, so `last()` returns each field at a *different*
   timestamp and the point a panel thinks it is reading never existed.

   Two consequences, and both were shipped as bugs before being caught in review:

   - **`pivot(rowKey: ["_time"], …)` splits into two rows** — one at the live tick, one at
     whenever the conditional field was last written, each blank where the other is filled.
     A table built by mapping over that renders every row twice for as long as the stale
     field stays inside the window. So a pivot must filter to fields written on every tick,
     and the moment a query needs a conditional field it needs guard 3 explicitly.
   - **A `last()` on a conditional field alone reads a value with no present tense.**
     `expires_at` outlives the command that set it, so counting it down turns a panel red
     and reports a healthy dispatcher as stopped after every normal release.

   The test is `exists` on the conditional field **and** on an unconditional one, which
   passes only when the same tick wrote both. Note this is not the same as testing
   `dispatch_active` — a loop that dies mid-command leaves both fields stale together, at one
   shared timestamp, and that case must still render. Distinguishing "no command" from "the
   dispatcher stopped" is the entire job of these panels, and the two look identical unless
   the query asks the question this way.

Note that `Released — following house` and `NO DISPATCHER` describe the *same register
contents* — `start=0`, the point §4.1 already flags. Only the freshness of the point
separates them, which is why guard 1 is not optional.

### 7.4 What this replaces

**"What to set in the app" (panel 8) must go in the same change.** It is the dashboard face
of `app_bands.py`, and §8 retires that. Leaving it up would put two contradictory
instructions on one screen — a table telling you to type thresholds into the app, directly
above a panel showing the dispatcher driving the same registers itself. Delete the panel when
`app_bands.py` is gated; reclaim its 8 rows of height for the decode table.

### 7.5 Mechanics

- **Edit `grafana/generate-battery-plan.py`, never the JSON.** The generator's own docstring
  says so and `tests/test_grafana_provisioning.py` re-runs it and diffs — a hand-edit fails
  the suite by design.
- **Bump the dashboard's top-level `version`.** Grafana skips a provisioned dashboard whose
  checksum is unchanged; `provisioning/dashboards/dashboards.yml` documents this at length
  after it bit this repo on 2026-07-28.
- **`dispatch_state` goes in the `alphaess` bucket, not `planning`.** It is an observation of
  hardware, same class as `power_readings`, and the dashboard already reads both buckets.

---

## 8. The app conflict

**The AlphaESS app writes the same dispatch register block.** App/cloud control and local
Modbus are not separate channels — handover §4, found holding `27` (= 10.8 %) in `0x0886`
matching the app's configured "discharge to 11 %".

Two things follow.

**The fail-safe is only safe once app control is off.** "Reverts to default behaviour" is
really "reverts to whatever the app is configured to do". If price-based control is left on,
every silence in this design — the freshness gate, a crashed dispatcher, a `self` slot —
hands the battery to a competing optimiser instead of to a neutral default.

As of the 2026-08-15 run it is **still on**: the very first sample reads
`start=1 mode=2 dpwr=−5000W dsoc=100.0% dt=5580s`, the app force-charging to 100 % at 5 kW
on a 93-minute command, releasing on its own mid-baseline. It did not corrupt that run — every
baseline after 16:11:41 shows `start=0` — but a 93-minute command that can assert at any
moment is a coin flip over any test, and once the dispatcher runs continuously the two fight
over one register block with the 60 s refresh as the only tiebreaker.

**`app_bands.py` must be retired or gated.** `Marstek-planning.py:2121` calls it every plan
run and writes sell-above/buy-below pairs into the `planning` bucket for a human to type into
the app. Its whole premise is "the app trades on one global price pair and cannot be given a
schedule, so retune it per session". That is the app's price-based control — the exact
mechanism this design replaces. They are not complementary layers; they are two answers to
the same question, fighting over one register block. Whatever replaces those rows in the
reports is a `battery-planning` concern, and it should land in the same change that ships the
translator.

---

## 9. Open questions

1. **Mode 1 with negative power — CLOSED 2026-08-16. It is a real, honoured, PV-only charge
   setpoint. The `self` row still does not change.**

   Two live runs, both `dispatch/test_mode1_negative.py --live`, because the first could not
   answer the question on its own.

   **Run A, 13:51 — overcommand.** 90 s at `mode=1 power=−4786 W target=76.4 %` against a
   ~2,790 W surplus. **DEMAND ruled out, decisively:** a demand reading predicts ~2,000 W of
   grid import; median grid was **+8 W** and no sample imported. Mode 1 with negative power
   does not force-charge from the grid.

   That run could not separate "capped at surplus" from "accepted and ignored", and no
   overcommand run ever could — above surplus both predict charge ≈ surplus, and charge as a
   share of surplus was 1.00 / 0.92 / 1.00 across baseline / during / after.

   **Run B, 14:22 — undercommand.** 240 s at `mode=1 power=−402 W target=83.2 %` against a
   ~1,220 W surplus, SoC 73.2 %. Ask for markedly *less* than surplus and the two hypotheses
   finally disagree, in the export channel:

   | | charge | export |
   |---|---|---|
   | setpoint honoured | ≈ 402 W | ≈ 820 W |
   | setpoint ignored | ≈ 1,220 W | ≈ 0 W |

   Observed: charge **331–361 W** and export **up ~1,014 W**. Both channels agree, and the
   decisive detail is the stability rather than the level — **PV swung 1,657–2,780 W across
   the window, moving surplus by 1,172 W (sd 403 W), while charge moved 30 W (sd 15 W).** A
   battery merely self-consuming tracks that surplus. This one tracked the setpoint and
   ignored the surplus.

   **What this makes Mode 1:** a charge command that is obeyed and that provably cannot
   import — the inverter exports rather than exceed the setpoint. That is a genuinely useful
   primitive, and §4.2's "Mode 1 and Mode 3 are empirically identical, both freeze at 0 W" is
   now known to be an artefact of sending `Pdispatch = 32000` exactly, which is not
   `< 32000`. The official table's qualifier was load-bearing.

   **What it does not make Mode 1 is self-consumption.** It exported over a kilowatt while
   the house drew ~500 W. So the `self` row stays a **dispatch release**, and the original
   plan's "if CAP, `self` becomes a real command" does not fire. The goldens do not
   regenerate. Note the conclusion held under either outcome: an ignored Mode 1 makes a
   command pointless, an honoured one makes it wrong.

   Confirmed at the register level across both runs: `0x0885 = 1` accepted and held, power
   and SoC target read back exactly as written, dead man's switch released cleanly.

   **Still untested:** whether Mode 1 honours its SoC target — reaching 83.2 % from 73.2 % is
   ~2.8 kWh and tens of minutes, far past either window, and it now matters because the power
   setpoint turned out to be real. Also unexplained: a single **~4.8 kW charge with 3,555 W of
   grid import** on the first sample after release at 14:27:05, gone by the next sample. One
   sample, ~10 Wh, but the dispatcher releases dispatch on every slot transition, so it is
   worth pinning down before that happens unattended.
2. **SoC drift across the 3-hour window.** `initialCharge` anchors the plan to real SoC at
   plan time, but by minute 170 the battery may have diverged (forecast error, the ≈300 W
   delivery mismatch). When live SoC and planned `soc_wh` disagree materially, does the
   dispatcher trust the power setpoint, trust the SoC target, or trigger an off-cycle replan?
3. **~~Max charge/discharge constants~~ — closed 2026-08-16, and not the way this entry
   expected.** The plan was to read the limits from `0x012C` / `0x012D` and need no constant
   anywhere. The first containerised dry run read them: **15,015 W charge, 13,728 W
   discharge.** On a 5 kW unit the planner tunes at 4,850 / 4,700.

   Those registers do not describe this system. A clamp trusting them alone passes every
   physically impossible command — which is the entire class of command the clamp exists to
   stop — so "reading it from the hardware means it cannot drift" bought nothing here.

   `slots.clamp()` now takes the **lower** of the reported limit and `HARD_MAX_POWER_W = 5000`.
   The hardware stays in the loop for the case that matters — a derate or a different unit
   reporting something *smaller* — and the constant binds the rest of the time. Reviewing that
   constant belongs to any future capacity change, since a bigger battery may arrive with a
   bigger inverter.

   Two corrections from the PR 75 review, both about what a *reported* limit means:

   - **0 is not "unknown".** `max_charge_w or hard_max_w` could not tell them apart, so a
     limit register reading 0 — the inverter refusing that direction outright — fell through
     to the 5 kW fallback. The one reading that means stop produced the largest command the
     clamp allows. A genuine 0 now refuses the command and holds instead; only `None`, a
     failed read, falls back.
   - **The limits are re-read hourly, not once at startup.** They move: 15,015 / 13,728 W in
     the morning of 2026-08-16 and 15,592 / 15,645 W the same afternoon. A container running
     for weeks against a number read at boot is clamping against history — and a derate, the
     case the clamp exists for, is precisely when that number changes mid-run.

   What is still worth doing, unchanged: log every interval that clamps. If it ever fires on a
   real plan, publish the planner's tuned figures on the `plan` point as `PLAN-repo-seams.md`
   §2a proposes for capacity, rather than raising the constant to make the warning go away.
4. **Behaviour past `horizon_end`.** The terminal reserve keeps charge in the battery at the
   end of the window, but the dispatcher needs a defined action when the plan runs out:
   silence, or an explicit hold. Silence loops back to §8.
5. **Atomic 9-register write.** The official spec writes all 9 registers in one FC `0x10`
   transaction; the current script does 5 separate writes and never writes reactive power.
   It works empirically for modes 1, 2 and 3. Worth aligning, low priority.
6. **`slots.json` hot-reload has never been tested against a live inverter.**

---

## 10. Deployment notes

- **Docker, not systemd.** Handover §9.5 assumes systemd units. This is Docker Compose on the
  NAS; containerise rather than introduce a third deployment mechanism. All `docker` commands
  on the Synology need `sudo`.
- **One service in this compose file.** Translator and dispatcher ship in one image. The
  translator runs on a timer inside it rather than as a second container, so `slots.json`
  never leaves the container's own volume and there is nothing to bind-mount across projects.
- **Deploy with `sudo docker compose up -d <service>`, never bare `up -d`.** A bare recreate
  in this stack cost 922 s of samples on 2026-08-10. The `sudo` is repeated here rather than
  left to the bullet above because this is the line that gets copied.
- **The single-connection limit is a deployment constraint.** Exactly one container may ever
  hold `:502`. A single compose service with a restart policy and no scaling is the
  enforcement.
- **Network.** The dispatcher needs LAN access to `192.168.68.151:502`. `alphaess-net`'s
  MTU 1400 was a TLS-handshake fix for the AlphaESS cloud API and is irrelevant to local
  Modbus TCP.
- **`slots.json` does not cross stacks.** It lives in a named volume owned by this service.
  An earlier draft bind-mounted a host directory shared with the planning stack; that is no
  longer needed now both halves are here. Keep it on a volume rather than inside the image so
  a rebuild does not discard the current slots.
- **Do not depend on the `alphaess-modbus` PyPI package at runtime.** Unmaintained since
  2022 and broken against current pymodbus. It is still useful as a data source — its
  `registers.json` is a 1025-entry register map. Talk to `pymodbus` directly, and keep the
  `inspect.signature` detection of `slave=` vs `device_id=`.
- **Reference docs:** `github.com/ramonvanraaij/ha-alphaess-modbus` carries the official
  AlphaESS PDFs in `docs/` and lists SMILE-G3-S5 as confirmed working. Treat the PDFs as
  ground truth and the README as good-quality community inference — where they disagree, the
  PDFs win.
