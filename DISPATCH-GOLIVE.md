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

**Update, 2026-08-17 (later still):** sections 2 and 3 are done bar the day-long watch and
the Kuma monitors. The dispatcher has been running in dry run since 18:05 and the §7.2
panels are confirmed on the dashboard.

Read the paragraph above again, though, because it is wrong in a way worth keeping: those
panels read `NO DISPATCHER` **whether or not** the service was running — see section 3's
third box. The reasoning was right and the conclusion was luck. A panel that displays the
expected string is not evidence until it has been seen displaying a different one.

Next action: **the full-day watch** (section 3, box four), reading `slot_action` and
`plan_run` rather than `action`. The Kuma monitors can be created alongside it, and #7 will
now stay green rather than firing, since the app's price control is off. Section 4 needs the
watch done first — it is the last chance to catch a wrong decision for free.

**Update, 2026-08-18:** the Kuma box is ticked — all seven monitors exist and are receiving
real heartbeats, which took a container recreate on top of creating them. Grafana was
restarted too, so the `Decision` panel (#100) is live and reads `hold`; it is the only tile on
the dashboard that moves during a dry-run day, and it is the answer to the third box's
ambiguity rather than another readback of it.

The full-day watch is the last open box in section 3. A partial run over the first night —
`--hours 12`, 21:52 to 07:38 — gives 558 ticks, 7 decisions, 4 faults and 6 previews, and
every one of the four faults is a network stall rather than a dispatch fault. That is the
shape to expect tonight, and section 3's box four now carries how to tell the two apart.

**Update, 2026-08-18 (later):** section 3 is finished. The full-day watch ran at 22:40 over
a true 24 h window; its two faults were traced to a dead `except OSError` in `scheduler.py`
that could never catch a pymodbus timeout, and both halves of that are fixed and tested.
Box four also gained the finding that a dry-run review can never show an evening discharge —
the drained battery removes it from the plan — so that criterion is retired rather than
waited on. 3,178 tests pass, ruff clean.

Next action: **section 4**, and it should be watched rather than left running. Two script
fixes worth
folding in on the way: `scripts/is-it-deciding.py` answers "is it deciding right now" in under
a second from the Mac, and the `GAP_S` constant it shares with `review-dry-run.py` needs to
stay in step with Kuma #5's interval.

**Update, 2026-08-18 (live):** section 4 is done — the dispatcher is writing to the inverter.
First live command 23:30, a Mode 3 hold at 0 W, `verified=True`, kill switch exercised and the
release confirmed in the log. Nothing in sections 1–4 is open.

The one thing to watch is not in this file: **tomorrow's 12:15 charge, 4,629 Wh**, the first
live command whose effect shows up in the power trace rather than only in a register. A hold
at 0 W is verifiable but invisible — the battery was already at 0 W — so it proves the write
path and nothing about the plan being right.

Next action: section 5 Part 3, the `plan-run` heartbeat, which lands in `battery-planning`.

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

- [x] **Turn off the AlphaESS app's price-based control**, and clear its price thresholds.
      The fail-safe is silence — stop writing, let the inverter revert — and that is only
      safe while nothing else drives the same registers. The app was caught doing exactly
      that on 2026-08-15: `dpwr=-5000W dsoc=100.0% dt=5580s`, a 93-minute forced grid charge
      at 5 kW. See `DESIGN-dispatch.md` §8. **Done 2026-08-17**: the app was set to
      self-sufficiency mode. Confirmed at the register level rather than in the app UI,
      which is the stronger check — the block read `Released` and stayed byte-identical
      across two reads twenty minutes apart. Monitor #7 catches a regression here
- [x] Confirm `d_start == 0` at rest once the app is off — **2026-08-17**, whole-block read
      via `registers.describe`: `0x0880=0` (Released), `0x0881` raw `32000`, which is
      `POWER_OFFSET` exactly and so decodes to 0 W with no residue, `0x0886=0`.
      `0x0887` held a leftover `90`, and a second read confirmed it static: with
      `d_start == 0` nothing acts on it, and it cannot leak into a later command because
      `Inverter.apply` writes the whole payload before arming `REG_START` (`scheduler.py:117`)
- [x] Mint `INFLUX_TOKEN_DISPATCH` — scope `r planning, rw alphaess`, per `DEPLOY.md`,
      "Scoped tokens". The only token in the stack that spans both buckets, which is why it
      is not the collector's. **Done 2026-08-17**, and it is the real minted token, not the
      `placeholder-not-yet-minted` string that unblocked the Grafana restart on the same day.
      Worth keeping the distinction: Grafana never reads this token, so the placeholder
      satisfies the stack-wide `:?` guard and everything looks healthy — the difference only
      shows up when the dispatcher tries to read the `planning` bucket in section 3
- [x] Put it and `INVERTER_IP` in the NAS `.env` — **2026-08-17**. Note this one cannot be
      confirmed by watching the dispatcher: `INVERTER_IP` is set to `192.168.68.151`, which
      is also the compose fallback in `docker-compose.yml`, so deleting the key from `.env`
      would change nothing observable. It was checked in the file, not inferred from a
      working connection
- [x] Give the inverter a DHCP reservation, if it does not have one — **2026-08-17**,
      reserved in the router. Worth doing rather than ticking optimistically: a moved lease
      makes the dispatcher silently stop connecting, and in dry run that is nearly
      invisible, because `action` already reads `no dispatch` all day — the same string a
      dead dispatcher produces. Monitor #5 is what catches it

## 3. Deploy, in dry run

- [x] `sudo docker compose up -d dispatch` — **never a bare `up -d`**, which recreates the
      collector and cost 922 s of samples on 2026-08-10. **Done 2026-08-17**
- [x] Confirm the container starts, writes `/data/slots.json`, and connects to the inverter —
      **2026-08-17**, `33 slots to 2026-08-18T22:00:00Z (charge=13 discharge=13 hold=7) from
      2026-08-17T18:05:04Z`, then `inverter limits: charge=15435 W discharge=15435 W`. Those
      limits sit inside the 15,015–15,645 W band `scheduler.py:68` records from 2026-08-16,
      which is the evidence for re-reading them hourly instead of caching at boot
- [x] Confirm `dispatch_state` is arriving in InfluxDB and the §7.2 panels are populated —
      **the InfluxDB half is confirmed, 2026-08-17.** All nine `raw_08xx` words plus
      `action`, `slot_action`, `plan_run`, `setpoint_w` and `duration_s` are landing, and
      the raw words decode back to the Modbus read exactly: `raw_0881/0882 = [0, 32000]` →
      0 W, matching `setpoint_w`. `expires_at` is correctly ABSENT — `state.py:103` emits it
      only while the block is active, and a dry run never arms it.

      **The Grafana half took two more PRs, and both bugs were in the panels rather than in
      the dispatcher** — which is the argument for looking at the dashboard as a deploy step
      instead of trusting the data. Both were only visible against a *resting* dispatcher,
      the state this whole section runs in, and neither could have been found in test:

      1. `Dispatch state` read `NO DISPATCHER` on a healthy dispatcher (#96). Grafana's
         field picker treats an empty `reduceOptions.fields` as auto, and auto is **numeric
         fields only** — `action` is a string, so it was dropped before any mapping was
         consulted and the panel fell to its `noValue`. Every mapping was unreachable and
         `NO DISPATCHER` was the only string it could ever display, which is precisely the
         distinction the panel exists to draw. `Commanded power` worked throughout because
         `setpoint_w` is numeric, and that is what made it look like a dispatcher fault
      2. Fixing that with `/.*/` then showed **too much** (#97): `/.*/` is every field, and
         Flux returns `_time` and the `sys_sn` tag next to `_value`, so the timestamp
         rendered as a second red tile with the serial on the action's label. The query now
         keeps only `_value`. Freshness is unaffected — `range(start: -5m)` enforces it by
         returning no rows, not through the timestamp column

      Also in #97: `Command expires in` was red all day. It yields nothing whenever no
      command is live — the resting state, and the whole of this section — and Grafana
      colours `noValue` with the **base** threshold step, which was red. That is the one
      panel whose red means the battery is a minute from acting unsupervised, so a
      permanently-red version of it is worse than no panel. Base is now neutral with red
      from 0, and the countdown is floored at 0 so a stalled loop stays red instead of
      counting past zero back onto the neutral base

      **Confirmed on the dashboard 2026-08-17**: `no dispatch` as a single tile, `0 W`,
      `0 %`, a grey `no command`, and the decode table agreeing with all of it
- [x] Watch a full day of dry-run decisions against what the battery actually did. This is
      the last chance to catch a wrong decision for free. Run it **after 22:30 local**, with
      `--hours 24` rather than the default: the default starts at local midnight, and the
      dispatcher's first tick was 21:52 on 2026-08-17, so only an explicit window covers a
      full day including an evening peak. The plan's last discharge block of the day runs
      22:00–22:15, which is why 22:30 rather than 22:00.

      **A gap fault is not automatically a dispatch fault.** Before treating one as a bug,
      query `power_readings` over the same minutes. If the collector stalled too it is the
      network, not the loop — the two use different transports and only something upstream of
      both stalls both. Overnight 2026-08-17/18 all four gap faults were of that kind.
      `review-dry-run.py` does this comparison itself since #102, so the page separates them.

      **And a real dispatch fault is not automatically a dispatch bug — but on 2026-08-18 it
      was one.** The two faults that survived the network cross-check, 13:02→13:05 and
      15:59→16:03, decomposed like this:

      - *The trigger was the inverter, not the loop.* pymodbus's frame trace shows
        transactions `0xeb`–`0xf2` all answered normally, then one request with no reply.
        Two consecutive ticks failed, ~12 s each (3 retries × ~3 s), which is what walked the
        tick phase `:33 → :45 → :57 → :58`. `docker inspect` gave `restarts=0`: the process
        was alive throughout. Live, both would have been harmless — ~145 s without a refresh
        against `DISPATCH_DURATION_S = 300`, so the command stays live and the dead man's
        switch never engages.
      - *The bug was ours.* The traceback died at `scheduler.py:288`, and line 289 is
        `except OSError` — a handler written to lose the SoC and decide without it.
        **`pymodbus.exceptions.ModbusIOException` does not subclass `OSError`**
        (`ModbusIOException → ModbusException → Exception`), so that handler, and the three
        like it, had never once fired. A read timeout — much the commonest Modbus failure —
        went straight past all of them into `run()`'s catch-all and killed the whole tick.
        Worst of the four is the verify read at `:358`, guarded by `if not inv.dry_run and
        wrote:`: the one error handler that exists **only on the live path** had never
        executed, and was broken. `Inverter.limits()` is worse in a different way — it is
        called before `run()`'s try, so an unreachable inverter at container start took the
        process down and `restart: unless-stopped` brought it back to try again.
      - *Fixed* by converting at the one boundary where pymodbus's exceptions enter the file
        (`Inverter.read`/`read_raw_block`/`write`), so all four handlers become real, and by
        publishing a degraded `dispatch_state` point carrying `slot_action`, `plan_run` and
        `read_error` when the inverter cannot be read. That second half is the one that
        matters for this box: without it an unreadable inverter and a dead dispatcher are the
        same shape in Influx — absence — which is exactly why these two took a container
        inspect and a log dive to tell apart.

      **Zero previews is the bad outcome, not the good one.** A preview is a divergence
      between the decision and what the battery actually did, which is exactly the behaviour
      change going live would buy; a day with none means the dispatcher is not worth
      deploying. Faults are what block go-live.

      **Read `slot_action` and `plan_run`, not `action`, while in dry run.** `action` will
      say `no dispatch` for the whole day even with a healthy dispatcher deciding every
      60 s: `describe_action` only says `self-consumption (released)` when the *decision*
      was `release`, and here the decision is `command` (a Mode 3 hold) that dry run
      declines to write. The readback is then indistinguishable from a crashed dispatcher —
      the exact ambiguity `state.py:44` flags, and the reason the decision is published
      rather than inferred from the registers in Flux. `verified=False` in the tick log is
      the same artefact and does NOT redden monitor #6: `scheduler.py:208` tests `dry_run`
      before the `verified is False` branch. Monitor #7 stays meaningful throughout, since
      a foreign command arms the block and `is_hijacked` catches an active block this
      process did not write

      **Done 2026-08-18 22:40**, window Mon 17 Aug 22:40 → Tue 18 Aug 22:40. 1,383 ticks,
      22 decisions, 24 plans, 2 faults, 8 network stalls, 17 previews. Both faults were the
      same defect and are fixed; see below. The previews are coherent — hold overnight to
      bank charge for the peak, three afternoon direction reversals where the plan wanted to
      buy while the house was drawing from the battery.

      **The evening-discharge criterion is unreachable under dry run, and waiting does not
      fix it.** Tick mix over the full day: `charge 209, discharge 27, hold 1147`. Twenty-
      seven minutes of discharge in twenty-four hours, all of it 08:00–08:30. The 19:30–21:30
      sell that the 14:05 plan promised never became a decision, and the reason is a ratchet
      this box did not anticipate: with dispatch writing nothing the battery self-consumes
      itself flat (38.4 % → 5.2 % over the day), every hourly re-plan reads that falling SoC,
      and a battery with nothing in it is never asked to sell. Each successive day therefore
      starts lower and produces *worse* evidence than the last. **Do not wait for an evening
      discharge to appear in a dry-run review.** It cannot, and the box is amended to say so
      rather than leaving the next reader watching for it.
- [x] Create the Kuma monitors, put each push URL in the NAS `.env`, and confirm each goes
      green — `DESIGN-dispatch.md` §6.1. **2026-08-17 evening, confirmed green 2026-08-18.**
      All seven are pinged by code now; an unset URL makes the ping a no-op, so a monitor left
      out of `.env` stays silent rather than failing:
  - [x] #2 `plan-in-influx` (2 h) and #3 `slots-written` (2 h) — pinged by the translator
  - [x] #4 `slots-fresh` (15 min) and #5 `dispatcher-alive`. Keep #5's interval above the
        60 s tick or one slow tick flaps it — **2 min was still too tight, see below**
  - [x] #6 `dispatch-confirmed` (5 min), #7 `inverter-not-hijacked` (5 min),
        #8 `soc-floor` (15 min). **#7 will fire, correctly, until the app's price control is
        off** — the block is being hijacked as of 2026-08-16. Create it last, or expect it red.
        In the event #7 has been green throughout, the app's price control having been cleared
        under section 2 first
  - [x] Set `SOC_FLOOR_PCT` to match the planner's `minBatterySOCPct`. Two repos, nothing
        comparing them
  - [x] **Do NOT create a TCP port monitor on `192.168.68.151:502`.** It would steal the
        inverter's single Modbus connection from the dispatcher — §6.2. None was created

      **Creating the monitors is not deploying them.** The push URLs go into `.env`, and the
      running container never re-reads `.env` — so all seven sat receiving nothing until
      `sudo docker compose up -d dispatch` recreated it. That recreate is what makes this box
      true, and it is a separate action from creating the monitors.

      **How the recreate was confirmed, because the obvious check does not work.** Looking for
      a restart-sized hole in the `dispatch_state` tick stream proves nothing: a recreate costs
      about 132 s, which is exactly the length of the ordinary slow ticks, so it hides in the
      noise. It was read as "never recreated" on that basis and that was wrong. What settles it
      is the heartbeat payload — #5 carrying `command: hold at 0 W` rather than only the manual
      `setup check` beat, since the container can only send that with the push URL in its
      environment, and the URL did not exist before the monitor did. A second, independent
      signal: the translator's hourly `plan_run` step moved from :52/:53 past the hour to
      :13/:14, and only a restart moves that phase.

      **Kuma's own trap:** setting Heartbeat Interval silently drags Retry Interval to the same
      value. Set Retry Interval *after* Interval, or it is not what the form shows.

      Also not automatic: the Telegram notification carries a "Default" badge but was **not**
      enabled on the new monitors. Tick it on each one.

      **#5 `dispatcher-alive` needs 180 s / retries 2 / retry 90, not 120 s.** At 120 s it
      logged four Down→Up flaps overnight 2026-08-17/18 — 02:17, 05:37, 07:05, 07:21 — for a
      dispatcher that never missed a decision. The stalls are real but they are not dispatch:
      `power_readings` has gaps in the same minutes, and the collector reaches the AlphaESS
      cloud over the WAN while dispatch reaches the inverter over the LAN, so only something
      upstream of both stalls both. The home network was unstable from the afternoon of
      2026-08-17. Worst observed stall is 276 s, so 180 s with retries 1 / retry 60 still
      alerts at 240 s; retries 2 / retry 90 rides it out and still catches a dead loop in
      6 min. `TICK_S`/`GAP_S` in `scripts/review-dry-run.py` and `scripts/is-it-deciding.py`
      are deliberately the same numbers — change all three together or they disagree about
      what a stalled loop is

## 4. Go live

- [x] Add `DISPATCH_LIVE=1` to `.env` — **not** to `docker-compose.yml`, which is tracked and
      guarded by `tests/test_dispatch_deployment.py`; editing it to go live would red the
      suite on every branch afterwards. **Done 2026-08-18, 23:30.**
- [x] `sudo docker compose up -d dispatch`, and **watch the first command land** — verify the
      readback matches what was written, not just that the log says "commanded".
      **Done 2026-08-18.** The first live command was a Mode 3 hold at 0 W, and the readback
      agreed with it on four consecutive ticks: `0x0880 Raw 1 Active`, `verified=True`.
      `verified` is the box's whole point — it is the field that separates "the log says
      commanded" from "the battery is doing it", and through the entire dry-run day it read
      `False` on every tick because nothing was written for it to confirm. This is the first
      time it has ever been observed `True`, which makes it the first evidence that the write
      path works end to end rather than the first evidence that it runs
- [x] Confirm release-on-exit works: `docker compose stop dispatch`, then check the block is
      inactive rather than counting down. **Done 2026-08-18.** `Stopped 0.4s`, and the log's
      last line is `dispatch released on exit` — so the release ran inside the grace period
      with two orders of magnitude to spare, and #104's `stop_grace_period: 30s` is margin
      rather than a fix for something observed.

      **Do not read the dashboard for this.** For up to five minutes after a stop the panels
      keep showing the last *commanded* state — `hold (battery frozen)`, a healthy countdown —
      because the publish is inside `tick()` while the release runs in `finally:` after the
      loop, so the release is never published. The last point is a live command by
      construction, and panels 20 and 23 both window five minutes and take `last()`. Read the
      log line, or `scripts/is-it-deciding.py`, and treat the tiles as five minutes stale.
      `DEPLOY.md`'s *Running the dispatcher* section is where this now lives for good

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
      really is in charge during the dry run. `DESIGN-dispatch.md` §7.4 has the reasoning.
      **Half done, 2026-08-20:** §7.4 counted one copy and there were two — the Overview
      dashboard carried "What to set in the app" and "Planned Actions in app" as well, and
      those are gone, replaced by the dispatch tile row. Battery Plan's panels 7 and 8 are
      still up and still blocked on TODO item 1, its unbumped `version`
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
