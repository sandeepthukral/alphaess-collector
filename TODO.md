# TODO

Small things worth doing, none of them blocking anything. This is the catch-all: items that
outlive a single branch but do not deserve a document of their own.

It is **not** a worklist. `DISPATCH-GOLIVE.md` and `CODE-REVIEW.md` are worklists — they have
a running order, a resume point, and boxes that get ticked in the same change as the work.
This file has no order. Delete a line when it is done rather than ticking it; the git history
is the record.

---

## Bugs

**1. The Battery Plan dashboard has changed twice without a `version` bump.**
`"version"` has read `17` since #97 (`2945739`), and both #100 (`89ddee6`) and `6e81f25`
edited `grafana/alphaess-battery-plan.json` after it. The generator's own comment says what
happens next: Grafana's file provisioner reads the new file, sees a version that is not
higher, and does nothing — no error, no log line, and recreating the container does not help.
It names the symptom too, which is the reason this is first on the list: *"a fix that appears
not to have worked, which sends you back to re-debug a query that was already correct."*
Bump it, and add the two missing numbered lines to the log above it (the log already skips
16 and 17).

**2. `review-dry-run.py` reports an ordinary Modbus timeout as a hijack.**
`scripts/review-dry-run.py:246`:

```python
armed = [t for t in ticks if (t.get("action") or "") != "no dispatch"]
```

A degraded point deliberately publishes no `action` at all — `dispatch/state.py:129-141` is
explicit that the honest report of an unreadable inverter is a missing field, not a stale one.
So `None` → `""` → `!= "no dispatch"` → counted as armed, and `kinds` on the next line renders
the literal string `"None"`. The result is the most alarming finding on the page — *the
dispatch block was ARMED by something that is not this dispatcher* — fired by the most
ordinary fault there is. The predicate wants `exists action and action != "no dispatch"`.

**3. `review-dry-run.py` never queries `read_error`.**
It pivots on `slot_action`, `action`, `plan_run`, `setpoint_w`, `dispatch_active` — none of
which a degraded tick writes when there is no active slot. Such a tick contributes no pivot
row and is invisible to the review entirely: not a gap, not a fault, not a stall. Adding
`read_error` to the field list closes it, and gives the page a third category it does not
have today — the loop alive and deciding, with the inverter unreadable.

**4. `tests/test_dispatch_goldens.py:330` string-sorts `plan_run`.**

```python
"runs": sorted(runs, key=lambda r: r["plan_run"]),
```

`plan.run_sort_key` exists precisely because these tags are not sortable as strings: runs
before 2026-07-30 carry `+02:00` where later ones carry `Z`. Use it.

---

## Dispatch

**4b. Battery Plan's decode table has no pinned column order.** Flux does not promise one and
`union` of five `map`s does not produce one, so the columns arrive in whatever order the
engine felt like — the Dispatch dashboard rendered the same query with `Means` first, which
puts the thing you read last in front of the register you were looking for. Every other
generated table in the repo pins this with an `organize` transformation; that one does not.
Fixed on `alphaess-dispatch.json`, not on `alphaess-battery-plan.json`, because the plan
dashboard needs item 1's version bump first.

**5. Panel 8, "What to set in the app", should have gone at go-live — on Battery Plan.**
`DESIGN-dispatch.md` §7.4 makes deleting it a go-live step and explains why: it is the
dashboard face of `app_bands.py`, which §8 retires, so leaving it up puts two contradictory
instructions on one screen — a table telling you to type thresholds into the app, directly
above panels showing the dispatcher driving the same registers itself. §7.4 time-boxed that
overlap to the dry run. The dry run ended on 2026-08-18. Panel 7, "Planned Actions in app",
is the same class.

Both were on the **Overview** dashboard too, which neither §7.4 nor this item noticed, and
that pair is gone as of 2026-08-20. What remains is Battery Plan's, and it is blocked on
item 1: deleting a panel there without bumping `version` past 17 changes the file and not
the dashboard, so the panels stay up and the deletion looks like it failed.

**6. Monitor #1, `plan-run`, still does not exist.**
`PLAN-repo-seams.md` Part 3, and the only change the dispatch feature makes to
`battery-planning`. That repo has no heartbeat and no Kuma reference anywhere; `plan-now.sh`
exits 1 on failure and nothing watches it. Dispatch now depends on fresh plans, so a silent
planning failure degrades to no dispatch at all within four hours, unwatched. Port the shape
of `send_heartbeat()` from `collector/collector.py:407`, rebuilt query string included.

**7. `battery-planning` finding E6 — retry the two InfluxDB POSTs.**
`getWithRetries` already exists there and covers the three outbound fetches. The Influx writes
are the last unretried network calls in the planner.

---

## Docs

**8. `DEPLOY.md`'s "The two battery-plan dashboards are generated, not exported" is out of
date in three ways.** It says "Five of the seven dashboards were exported"; it is eight
dashboards and three generators, and `generate-energy-losses.py` is not mentioned as
generated anywhere in the file. The section's whole job is to stop someone hand-editing a
generated JSON, so a reader who checks it against the directory and finds it wrong learns to
distrust it.

---

## Housekeeping, on the NAS

**9.** `/tmp/recover-kuma-urls.py` can go — it did its job on 2026-08-17.

**10.** `.env.swp` still sits beside `.env`. That swap file is what caused the recovery that
blanked seven Kuma monitor URLs on 2026-08-17, silently, because compose passes `${X:-}` and
`send_heartbeat` returns on empty. Delete it before vim offers to recover it a second time.
