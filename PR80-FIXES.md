# PR #80 review — fix list

Branch `dispatch-docs`, diff `main...HEAD`. Verified before review: full suite green
(3,009 passed, 1 skipped), `python3 grafana/generate-battery-plan.py` reproduces
`grafana/alphaess-battery-plan.json` byte-for-byte, grid positions contiguous
(y=0/4/8/14/23/32/40), DST golden correct.

**No correctness bug was found in the new Python or Flux.** Findings 1, 7 and 8 are code;
the rest are contradictions between the newly committed docs and the code they describe.
Fix in this order — 1 is the only one that must land before merge.

---

## 1. `DISPATCH-GOLIVE.md:107` — go-live step 1 permanently reds the test suite

The checklist says:

> - [ ] Add `--live` to the `dispatch` service's `command:` in `docker-compose.yml`

`docker-compose.yml` is tracked, and `tests/test_dispatch_deployment.py:26`
(`test_the_service_starts_in_dry_run`) asserts `"--live" not in DISPATCH["command"]`.
So performing §4 as written makes `pytest` fail on every subsequent branch until someone
deletes or weakens the guard.

The test's intent is right and must survive: going live is an operator action taken while
watching the dashboard, not something that arrives by merge. So change the *mechanism*,
not the test's meaning.

- `docker-compose.yml`, dispatch service — current:
  `command: ["--ip", "${INVERTER_IP:-192.168.68.151}"]`
  Change to something env-driven, e.g.
  `command: ["--ip", "${INVERTER_IP:-192.168.68.151}", "${DISPATCH_LIVE_ARG:-}"]`
  (verify the empty-string element is tolerated by `dispatch`'s argparse; if not, use an
  entrypoint-level `DISPATCH_LIVE=0|1` that appends the flag.)
- `.env.example` — add `DISPATCH_LIVE_ARG=` (empty = dry run) with a comment saying the
  empty default is the safe one and that setting it is the go-live action. Note
  `.env.example:15` also currently repeats the "add `--live` to compose" instruction;
  update it to the new mechanism.
- `tests/test_dispatch_deployment.py:26` — reassert the *default*: the committed compose
  file must not hardcode `--live`, and the live arg must come from an unset-by-default
  variable. Keep the docstring's reasoning.
- `DISPATCH-GOLIVE.md:107` — rewrite the step to set the variable in `.env` and restart,
  not to edit a tracked file.

Cross-check while here: `docker-compose.yml`'s own comment about the heartbeat URLs says
"…before `--live` is added", and MEMORY notes `up -d` recreates the collector — the step
already scopes to `up -d dispatch`, which is correct; keep that.

## 2. `DESIGN-dispatch.md:688` — §7.2 stat table lists 5 states, code emits 7

`dispatch/state.py:describe_action()` returns exactly these seven strings:

```
charging from grid
charging from PV
discharging to grid
hold (battery frozen)
self-consumption (released)
no dispatch
active at 0 W
```

§7.2's table lists five, in different wording: `Charging from grid` / `Discharging to
grid` / `Holding` / `Released — following house` / `NO DISPATCHER`.

This set is a contract — `state.py:60-65` says so explicitly: any string without a value
mapping renders in the base colour, which is the red reserved for NO DISPATCHER. Someone
building the mappings from the doc's list would paint `charging from PV`,
`self-consumption (released)` and `active at 0 W` as failures.

The shipped `alphaess-battery-plan.json` is correct and pinned by
`test_the_state_stat_maps_every_action_the_dispatcher_can_emit`. **Only the doc is wrong.**

Fix: replace the table cell with all seven emitted strings verbatim (lowercase, as
emitted), each with its rendered display text and colour. Add a sentence pointing at
`state.describe_action()` as the source of truth and at the test that pins the two sides
together. Note §7.3 (`DESIGN-dispatch.md:757`) also names `Released — following house` and
`NO DISPATCHER` — keep that passage's *point* (both are `start=0`, only freshness
separates them) but use the real strings.

## 3. `DESIGN-dispatch.md:457` — §5 step 6 states the sign convention inverted

```
charge     -> mode 2, power = -power_w, target_soc
discharge  -> mode 2, power = +power_w, target_soc
```

That is the **register** convention. `registers.Command` / `slots.decide()`
(`dispatch/slots.py:206,214`) and §7.1's `setpoint_w` use the opposite, charging-positive
convention — `dispatch/state.py:build_fields` comments that `registers.decode_power`
already flipped it, and panel 9 negates `battery_power_w` in Flux to match.

`registers.py`'s docstring names this exact flip as the thing that "gets silently wrong"
and writes a plausible number to real hardware.

Fix: say which layer the step's signs belong to. Either annotate step 6 as operating in
register space (and add one line naming charging-positive as the convention everywhere
above the register boundary), or restate it in charging-positive terms and move the flip
into the `Encode:` line below it. Do not change any code — the code is consistent.

## 4. `DESIGN-dispatch.md:215` — §4 step 1's query window contradicts the implementation

Doc: `from now-rounded-down-to-quarter to +36h`.
Code (`dispatch/translate.py:258-259`): `LOOKBACK = 1 hour`, `LOOKAHEAD = 48 hours`.

The one-hour lookback plus the `upcoming()` trim is load-bearing: it is what keeps the
current interval when a run is slow, covered by
`test_the_current_interval_is_kept_even_when_nearly_over`. A quarter-aligned start would
drop that margin.

Fix the doc to `now − 1 h` to `now + 48 h`, and add the one clause saying the lookback
exists so a slow run does not lose the interval it is currently in (with the test name).

## 5. `dispatch/translate.py:145` — `newest_run()` sorts two timestamp formats as strings

```python
runs = sorted(iv.plan_run for iv in intervals if iv.plan_run)
return runs[-1] if runs else ""
```

Tags written before 2026-07-30 carry a `+02:00` offset; later ones end in `Z`. A
lexicographic sort orders those wrongly against each other — which is exactly why
`plan.run_time()` exists (`plan.py:287-300`) and why `newest_by_interval` parses instead
of sorting strings. The corpus still contains both forms
(`dispatch/testdata/run_20260730T172614p0200.json` vs `run_20260730T180504Z.json`), so
this is reachable, not theoretical.

Impact: a window containing an old-format tag makes monitor #2's "up" message name the
wrong plan run — the one string an operator reads on their phone to decide whether the
battery is following a stale plan.

Fix: sort by `plan.run_time(tag)` rather than by the raw string. The docstring says this
uses "the same rule `build_document` uses", so **check `translator.build_document`'s
`plan_run` selection and fix both together** — if they diverge, the docstring's promise
breaks. Add a regression test with one `p0200` tag and one `Z` tag where the string sort
and the parsed sort disagree.

## 6. `tests/test_dispatch_goldens.py:182` — regeneration silently drops reviewed runs

The docstring claims "re-fetching a newer archive cannot erase the review history", but:

```python
for plan_run in reasons:
    intervals = by_run.get(plan_run)
    if intervals is None:
        continue
```

Any run present in `reviewed_runs.json` but absent from the local corpus is dropped from
the rewritten file entirely — reason, digest and all. Regenerating against a partial
corpus loses review history permanently. It is caught only indirectly by
`test_the_reviewed_list_is_intact`'s `len(runs) == 9`, i.e. *after* the file was
overwritten.

Fix: on `intervals is None`, carry the existing entry through unchanged rather than
`continue` (it still has its reason, slots, actions, warnings and digest from
`existing`), or fail loudly if a reviewed run cannot be re-derived. Carrying through is
the behaviour the docstring already promises. Make the docstring match whichever is
chosen.

## 7. `DESIGN-dispatch.md:763` — §7.4's imperative is violated by this same PR

> **"What to set in the app" (panel 8) must go in the same change.** … Delete the panel
> when `app_bands.py` is gated; reclaim its 8 rows of height for the decode table.

Panel 8 still ships (`gridPos y=32, h=8`), the decode table took its own 6 new rows, and
`DISPATCH-GOLIVE.md:166` defers the deletion to go-live ("Delete Grafana panel 8 when the
app's price bands stop mattering (a go-live change)"). So the doc issues an imperative
this PR breaks, another doc in the PR contradicts it, and the hazard §7.4 names — two
contradictory instructions on one screen — is what actually ships.

Fix (doc only, unless you want to delete the panel now): reconcile the two documents.
Either soften §7.4 to match the go-live deferral and say plainly that the overlap is known
and time-boxed to go-live, or delete panel 8 in this PR and reclaim the rows. Also drop
the "reclaim its 8 rows for the decode table" clause — the decode table already has its
own rows, so that sentence is stale either way.

## 8. `DESIGN-dispatch.md:316` and `:27` — harvest-rule numbers contradict each other

Three statements of the same measurement disagree:

- `:316` — "across the **137-run** archive it fires … on **13 of 16 days**"
- `:325` — deduplicated to newest run per interval, "57 intervals across **15 of 16 days**"
- `dispatch/translator.py:118` — "over the whole archive it fires … on **15 of 16 days**"

The deduplicated set is a subset of the intervals, so it cannot cover *more* days than the
full set — 15 > 13 is impossible. At least one number is wrong.

`:27` also says "138-run archive" against `:316`'s "137-run".
**`dispatch/testdata/manifest.json` has `run_count = 138`, so 138 is correct and `:316` is
the typo.**

Fix: correct `:316` to 138, then re-derive the day counts from the corpus and make all
three statements agree. This is the evidence the harvest rule is justified by, so a reader
re-deriving it currently gets a different answer than the code comment asserts.
