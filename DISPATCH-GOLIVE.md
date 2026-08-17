# Dispatch — what is left before, during and after go-live

The worklist for finishing `DESIGN-dispatch.md`. Same convention as `CODE-REVIEW.md`: **tick
the box and add the note in the same change**, so this file always says where we actually got
to rather than where we meant to get to.

Design lives in `DESIGN-dispatch.md`. The repo-boundary and test-strategy reasoning lives in
`PLAN-repo-seams.md`. This file is only the running order.

## Resume point, 2026-08-16

Built and verified in dry run, nothing committed, nothing deployed. The whole path works end
to end from inside the container image, against the real inverter and the real `planning`
bucket: 46 slots written, inverter read, direction guard correctly refused a discharge to
10.0 % against a live SoC of 10.4 %, dispatch released on exit.

3,009 tests pass, ruff clean, `docker compose config` passes with `.env.example`.

**Update, 2026-08-16 (later):** section 1 is done. Six PRs open, #75–#80, stacked in merge
order, and the PR 75 review fixes are folded into the branches they belong to rather than
carried as a follow-up. 3,018 tests pass, ruff clean, `docker compose config` passes with
`.env.example`.

**Update, 2026-08-17:** every PR from section 1 is merged (#75–#87), the stale branches are
deleted, and `delete_branch_on_merge` is on in both repos. Section 5's Part 2 is finished —
both seams closed, capacity now travelling with the plan and the `planning` schema written
down and tested. 3,124 tests pass, ruff clean on the CI paths.

**Update, 2026-08-17 (later):** the capacity work is deployed and confirmed on the NAS.
`alphaess-dashboard.json` reads `Capacity (Wh) 27900` from the plan, and both SoC traces
render as percentages against it. Getting there needed one unplanned fix (#89): the dispatch
service has been in `docker-compose.yml` since section 1, its `INFLUX_TOKEN_DISPATCH` guard
is stack-wide, and so a `.env` without that key blocked `docker compose restart grafana` — a
command touching neither dispatch nor Influx. The variable is now in `DEPLOY.md`'s token
table with the `influx auth create` that mints it, and `tests/test_compose_env_guards.py`
fails if a future `:?` guard is added without both. Note what CI cannot do here: `ci.yml`
copies `.env.example`, which carries a placeholder for every key, so any new guard is green
in CI and blocking on the NAS. The check that generalises is not "does it boot".

The dispatch panels read `NO DISPATCHER` / `No data`, which is correct — the service has
never been started, and section 3 is what starts it.

Next action: **section 2**, which is the part that needs you at a keyboard on the NAS —
nothing in section 3 can start until it is done, and turning off the AlphaESS app's price
control gates all of it. The only code work left in section 5 is Part 3's heartbeat, which
lands in `battery-planning`, not here.

---

## 1. Get it into version control

- [x] Branch off `main` — never continue on a merged branch. **2026-08-16**: six branches,
      each stacked on the one before, because each depends on it (`pyproject.toml`'s
      `pythonpath`, then `corpus.py`, then the goldens that import it).
- [x] Split into PRs by "one reason to revert". Six, not the five first sketched — the
      Grafana dispatch row is its own revert:
  - [x] `dispatch-core` — registers, plan, translator, slots, state, scheduler + tests.
        Also `tests/fixtures/plans/`, which belongs here rather than with the goldens: the
        synthetic *inputs* are what the translator tests read
  - [x] `dispatch-corpus` — the two `scripts/`, `corpus.py`, the `dispatch/testdata/`
        gitignore entry, and the archive-wide invariant sweep
  - [x] `dispatch-goldens` — `tests/fixtures/golden_slots/`, `test_dispatch_goldens.py`
  - [x] `dispatch-deploy` — `Dockerfile`, `entrypoint.sh`, `translate.py`, the compose
        service, `.env.example`, `test_dispatch_deployment.py`
  - [x] `dispatch-panels` — the section 7 dashboard row
  - [x] `dispatch-docs` — `DESIGN-dispatch.md`, `PLAN-repo-seams.md`, this file
- [x] Confirm `dispatch/testdata/` is ignored — checked with `git check-ignore -v` **before**
      anything was staged, and a dry-run `git add dispatch/` confirmed 12 files, all code.
      It carries household plan data and this repo is public
- [x] Each commit verified green on its own, not just at the end: 470 tests at
      `dispatch-core`, 2,959 at `dispatch-corpus`, 3,009 at `dispatch-panels`, ruff clean
      throughout. The sweep also checked both ways — 2,489 tests with the corpus present,
      23 skips without it, which is the CI case
- [x] **Push and open the PRs** — six, #75–#80, 2026-08-16. Merge in stack order
- [x] Act on the PR 75 review. Four findings were real and are fixed in the stack: monitors
      #4–#8 were documented and never pinged; `clamp()` read a 0 W hardware limit as
      "unknown"; the idle path published a pre-release readback; the inverter limits were read
      once and held forever. Two more were declined and why is worth keeping — the duplicated
      capacity constant is tracked as `PLAN-repo-seams.md` §2a and is not a new finding, and
      `entrypoint.sh` forking a fresh translator process per interval is deliberate: a batch
      job that leaks or wedges is contained by its own exit
- [x] A hazard the review did not raise, found while staging the fixes: `dispatch/testdata/`
      was only gitignored from the *third* commit in the stack, so a `git add -A` from either
      of the first two would have staged the real plan archive into a public repo. Moved to
      the first commit
- [x] Re-confirm `dispatch/testdata/` is still ignored after the merges — **2026-08-17**,
      `.gitignore:234` still matches and `git ls-files dispatch/testdata/` is empty, so
      nothing slipped in while the six branches were being merged and deleted

## 2. Prerequisites for going live

These are the ones that need a human and a NAS. **The app one is the real gate.**

- [ ] **Turn off the AlphaESS app's price-based control**, and clear its price thresholds.
      The fail-safe is silence — stop writing, let the inverter revert — and that is only
      safe while nothing else drives the same registers. The app was caught doing exactly
      that on 2026-08-15: `dpwr=-5000W dsoc=100.0% dt=5580s`, a 93-minute forced grid charge
      at 5 kW. See `DESIGN-dispatch.md` §8.
- [ ] Confirm `d_start == 0` at rest once the app is off
- [ ] Mint `INFLUX_TOKEN_DISPATCH` — scope `r planning, rw alphaess`, per `DEPLOY.md`,
      "Scoped tokens". The only token in the stack that spans both buckets, which is why it
      is not the collector's
- [ ] Put it and `INVERTER_IP` in the NAS `.env`
- [ ] Give the inverter a DHCP reservation, if it does not have one

## 3. Deploy, in dry run

- [ ] `sudo docker compose up -d dispatch` — **never a bare `up -d`**, which recreates the
      collector and cost 922 s of samples on 2026-08-10
- [ ] Confirm the container starts, writes `/data/slots.json`, and connects to the inverter
- [ ] Confirm `dispatch_state` is arriving in InfluxDB and the §7.2 panels are populated
- [ ] Watch a full day of dry-run decisions against what the battery actually did. This is
      the last chance to catch a wrong decision for free
- [ ] Create the Kuma monitors, put each push URL in the NAS `.env`, and confirm each goes
      green — `DESIGN-dispatch.md` §6.1. All seven are pinged by code now; an unset URL makes
      the ping a no-op, so a monitor left out of `.env` stays silent rather than failing:
  - [ ] #2 `plan-in-influx` (2 h) and #3 `slots-written` (2 h) — pinged by the translator
  - [ ] #4 `slots-fresh` (15 min) and #5 `dispatcher-alive` (2 min). Keep #5's interval
        above the 60 s tick or one slow tick flaps it
  - [ ] #6 `dispatch-confirmed` (5 min), #7 `inverter-not-hijacked` (5 min),
        #8 `soc-floor` (15 min). **#7 will fire, correctly, until the app's price control is
        off** — the block is being hijacked as of 2026-08-16. Create it last, or expect it red
  - [ ] Set `SOC_FLOOR_PCT` to match the planner's `minBatterySOCPct`. Two repos, nothing
        comparing them
  - [ ] **Do NOT create a TCP port monitor on `192.168.68.151:502`.** It would steal the
        inverter's single Modbus connection from the dispatcher — §6.2

## 4. Go live

- [ ] Add `DISPATCH_LIVE=1` to `.env` — **not** to `docker-compose.yml`, which is tracked and
      guarded by `tests/test_dispatch_deployment.py`; editing it to go live would red the
      suite on every branch afterwards
- [ ] `sudo docker compose up -d dispatch`, and **watch the first command land** — verify the
      readback matches what was written, not just that the log says "commanded"
- [ ] Confirm release-on-exit works: `docker compose stop dispatch`, then check the block is
      inactive rather than counting down

## 5. Cross-repo seams — `PLAN-repo-seams.md` Parts 2 and 3

Not blocking go-live. They make it safe to *change* things later, which is a different
problem and a real one.

- [x] **Publish capacity into InfluxDB** (Part 2a). **Done 2026-08-17**, both halves. It was
      written by hand in nine places across two repos and two units (`27.9` kWh for the
      collector, `27900` Wh for everything else); the planner now publishes it on every
      `plan` point and all three dashboards read it from there. What remains hardcoded is
      `.env`'s `BATTERY_CAPACITY_KWH`, deliberately — `pricing.py` needs it with no plan in
      hand — and `hardware.py`'s constant, which is the planner's own default. No change to
      the number is scheduled — 27,900 is the commandable capacity, and the ~30,500 Wh in
      `hardware.py` is headroom the pack only reaches after a long hold at 100 %. The reason
      to do it anyway: the dashboards divided one repo's `soc_wh` by the other's copy of the
      constant, so any half-done change rendered as a plausible, wrong percentage rather than
      an error — and `alphaess-dashboard.json`, the one we will be watching the dispatcher
      on, had no generator and no test guarding its copy. It now has the second of those.
  - [x] `battery-planning`: add `capacity_wh` as a field on each `plan` point — **PR #25
        over there, 2026-08-17.** It publishes `ratedBatteryCapacity`, **not**
        `hardware.CAPACITY_WH`: `BT_CAP` and the Domoticz user variable both override it, so
        the default is not necessarily what a given plan was optimised against, and on a
        backtest it is wrong every time. Publishing the default would have moved the same
        mismatch one layer down and hidden it better
  - [x] here: the dashboards read it from the plan instead of hardcoding it — **2026-08-17.**
        Unblocked by the deploy: `capacity_wh` confirmed arriving at `27900`, single distinct
        value over the last day, with all thirteen pre-existing fields intact. All **three**
        dashboards changed, not the two with generators — `alphaess-dashboard.json` has no
        generator and is hand-edited, and leaving it on the literal would have been the exact
        half-done state this item exists to prevent, on the dashboard we will be watching the
        dispatcher on. The variable went `textbox` → `query`; `int()` in the query is load-
        bearing, since every consumer interpolates it as `float(v: ${capacity_wh})` and the
        field is a float. `tests/fixtures/planning_schema.json` had to declare `capacity_wh`
        first: the schema test walks *any* `"query"` key, so a Flux template variable is
        checked against the fixture exactly like a panel is
  - [x] extend `tests/test_grafana_provisioning.py` to cover `alphaess-battery-score.json`
        — **2026-08-17.** It now *discovers* every dashboard carrying a `capacity_wh`
        variable rather than naming two by hand, which is what let the score dashboard sit
        outside the check while dividing by `27900` on thirteen panels. Three are found
        today, and a guard fails if fewer are, so the loop cannot go vacuous. A second test
        pins each generator against the dashboard it generates
  - [ ] also review `slots.HARD_MAX_POWER_W` when the battery changes — a bigger battery may
        arrive with a bigger inverter
- [x] **Commit the schema fixture** (Part 2b). **2026-08-17.**
      `tests/fixtures/planning_schema.json` plus `tests/test_planning_schema.py`. Both halves
      now exist: a renamed field raises at runtime and names itself to monitor #2, and an
      *undeclared* read fails in CI.
  - Three things the writing of it turned up, none of them expected:
    - The bucket holds five measurements, not one. This repo reads three (`plan`,
      `app_setting`, `plan_score`) and ignores `weather_forecast` / `weather_observed`.
    - `plan.price_market` and the `market_price` measurement are **different things** — the
      latter is in the `alphaess` bucket, and the price panels union the two. A field
      extractor that reads a query as belonging to one bucket attributes one to the other;
      the one here splits on `from(bucket:)` boundaries for exactly that reason.
    - `plan.load_forecast_wh` is in the bucket. It is occupancy at 15-minute resolution and
      this repo is public, so the fixture asserts it is declared **nowhere** — the same rule
      `dispatch/corpus.py` applies at the parse boundary, one layer further out.
  - The fixture records what this repo DEPENDS ON, not what the planner writes. Field lists
    were verified against the live bucket on 2026-08-17; the tests deliberately do not
    re-query it, since CI has no NAS and a test that skips without one guards nothing.
- [ ] **One heartbeat in `battery-planning`** (Part 3). Monitor #1 `plan-run`, pinged by
      `plan-now.sh`. That repo has no Kuma reference anywhere today and `plan-now.sh` exits 1
      with nothing watching. Once dispatch depends on fresh plans, a silent planning failure
      degrades to "no dispatch at all" within four hours. Its own PR over there

## 6. Deferred, and fine to stay deferred

- [x] `dispatch/test_mode1_negative.py --live` — **run 2026-08-16 13:51.** Grid import ruled
      out (median +8 W against a command 2 kW above surplus), so DEMAND is dead and Mode 1 is
      harmless. CAP unproven from this run alone: the battery tracked surplus identically
      before, during and after, so "accepted and ignored" fitted every sample
- [x] `--undercommand` — **run 2026-08-16 14:22, and this one settles it.** 402 W commanded
      into a ~1,220 W surplus: charge held 331–361 W while PV swung 1,657–2,780 W, and export
      rose ~1,014 W. Surplus moved 1,172 W, charge moved 30 W. **Mode 1 is a real, honoured,
      PV-only charge setpoint** — it exports rather than exceed the setpoint, so it can never
      import. **§4.1's `self` row still stays a release** (an obeyed setpoint is a charge
      command, not self-consumption) **and the goldens do not regenerate.** See §9 question 1
- [ ] Explain the release transient: one sample at 14:27:05, immediately after `start=0`,
      read **−4,791 W battery with +3,555 W grid import**, gone by the next sample 11 s later.
      ~10 Wh and internally consistent (load reconciles at 694 W), so probably real rather
      than a read artefact. The dispatcher releases on every slot transition, so this wants
      pinning down — sample at 2 s across a release, or cross-check the collector's own
      Influx series, which measures the same inverter independently
- [ ] Whether Mode 1 honours its **SoC target** is still untested and now matters, since the
      power setpoint turned out to be real. ~2.8 kWh from 73 % to 83 %, so it needs its own
      long run rather than a 4-minute window
- [ ] Delete Grafana panel 8 ("What to set in the app") once `DISPATCH_LIVE=1` is in and the
      app's price bands stop mattering. Until then it is the panel that is *correct* — the app
      really is in charge during the dry run. `DESIGN-dispatch.md` §7.4 has the reasoning
- [ ] Plan-vs-actual reconciliation, monitor #9 (§5.4) — the daily job that catches "every
      monitor green, battery not following the plan"
- [ ] `slots.json` hot-reload has never been tested against a live inverter (§9.6)
- [ ] Decide whether to record the `self`-overspends-the-plan gap in `DESIGN-dispatch.md`.
      `self` means "let self-consumption do whatever it does", not "discharge exactly the
      planned Wh" — on 2026-08-16 23:45 the plan modelled 82 Wh from the battery while
      self-consumption would cover the full 235 Wh house load. Benign in every case in the
      archive (3 real instances, all at the end of a horizon where it saves money), but
      nothing prevents a plan that deliberately rations the battery mid-horizon

## Not in this file

`CODE-REVIEW.md` is its own worklist and still has #3 (hardcoded bucket in dashboards),
#4 (`daily-savings.sh` date parsing) and #8 (Flux parameter binding) open.
