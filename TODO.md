# TODO

Small things worth doing, none of them blocking anything. This is the catch-all: items that
outlive a single branch but do not deserve a document of their own.

It is **not** a worklist. `DISPATCH-GOLIVE.md` and `CODE-REVIEW.md` are worklists — they have
a running order, a resume point, and boxes that get ticked in the same change as the work.
This file has no order. Delete a line when it is done rather than ticking it; the git history
is the record. The numbers are stable labels, not a sequence — a gap means that item is done,
and renumbering would break every reference to it in a commit message or a PR.

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

**11. Nothing alarms on a permanently silent cell temperature.**
`dispatch/scheduler.py` step 8b degrades a failed or implausible temperature read to no
fields, warns once, and drops the repeats to debug — deliberately, because it is
observability and must never cost a tick. The consequence is that a block which stops
answering forever looks exactly like a healthy one from every monitor: the tiles read
"unreadable", the chart flatlines into the past, and nothing says so. `dispatch/reliability.py`
is where that check belongs (it already owns "these fields should be present and are not"),
severity no higher than a warning — a missing temperature is not a reason to touch the
battery. Deliberately left out of #128, which is the change that created the field: an alarm
on a field with no history to calibrate against would be a threshold picked from nothing.
Raised in that PR's review.

**12. The health-poller's cell-voltage block needs its base address confirmed before it can be
added.** `registers.py`'s own comment on the temperature block (lines 83-88) cites Alpha2MQTT
documenting the cell-voltage block at `0x0106`-`0x010A` (5 words), copied there specifically as
the source that got miscopied onto the temp block's own scale comment. The battery-health
dashboard's handover instead specifies `0x0105`-`0x010A` (6 words) for this block, mirroring
`TEMP_BLOCK`'s pack/cell/value-times-two layout. A one-register base-address error would shift
every field in the block, the same class of mistake this module's docstring opens with (the
0x0883 anecdote). Resolve by reading both candidate base addresses against the live inverter
with `--once` and comparing to the app's own displayed cell voltages, the same way the temp
block's scale was confirmed on 2026-08-15/08-27, then add `decode_voltage_block`/
`voltage_plausible` next to `decode_temp_block` and wire it into the health-poller's hourly
gate (`HEALTH_REFRESH_S`, `dispatch/scheduler.py` step 8c).

**13. The health-poller's daily tier (SoH, lifetime energy, heatsink temp, PV energy) has no
confirmed register layout.** The battery-health dashboard's handover lists `0x011B` (SoH),
`0x0120`-`0x0125` (lifetime charge/discharge/grid-charge energy, likely 3 metrics x 2 words
each for 32-bit values but word order and scale unconfirmed), `0x0435` (inverter heatsink
temp), and `0x043E`/`0x08D2` (inverter/system lifetime PV energy) — none of these appear
anywhere else in this repo, and no register-reference document backing them was found (the
existing constants trace to Alpha2MQTT/ha-alphaess-modbus citations in `registers.py`'s
comments; these new ones don't). Needs either an authoritative reference or live-inverter
verification — SoH and lifetime energy totals are both visible in the AlphaESS app, so a
`--once` read cross-checked against the app's own numbers is the same verification path used
for the temp/voltage blocks — before `decode_soh`/`decode_energy_block`/etc. can be written.
Once confirmed, add a `DAILY_HEALTH_REFRESH_S` gate (`~86400s`) next to `HEALTH_REFRESH_S`.

**14. The health-poller's weekly firmware/system-config blocks are published as raw hex, not
decoded fields.** `FIRMWARE_BLOCK` (`0x0115`-`0x011A`), `INVERTER_FW_BLOCK`
(`0x0640`-`0x0653`), and `SYSTEM_CONFIG_BLOCK` (`0x0800`-`0x080F`) are read and published
verbatim (`firmware_raw_*`/`inverter_fw_raw_*`/`system_config_raw_*`, hex-keyed) rather than
as the named fields the battery-health dashboard's handover describes (BMU/LMU/ISO firmware
version, battery capacity/type, max feed-into-grid %, PV capacity settings, system mode,
battery-ready flag) — which individual words within each block correspond to which named
value isn't confirmed anywhere in this repo. Row 5 of `alphaess-battery-health.json` shows raw
register/value pairs for now, same "checkable, not decoded" treatment the handover already
sanctions for the already-shipped fault/warning table (`FAULT_BLOCK`, raw words only — see
item 15 on why it has no derived count either). Decode individual fields once confirmed; no
gate/cadence change needed, just extending the existing `decode_firmware_block`/
`decode_inverter_fw_block`/`decode_system_config_block` functions in `registers.py`.

**15. `FAULT_BLOCK` (`0x0131`-`0x0146`) has no derived "how many faults are active" summary,
and needs one confirmed live before it gets one.** Raised in PR #129's review: a naive
`sum(1 for w in words if w != 0)` looked safe because it needs no bit-level knowledge to
compute, but the range itself is not confirmed to be fault/warning bits exclusively — if even
one word in it is a normally-nonzero status value, counter, or nameplate figure rather than a
fault flag, that count pins itself at 1 or more permanently, which reads on the health
dashboard as "faults active" forever and is worse than publishing no summary at all, because it
looks confident. Resolve the same way as items 12/13: read the block live with `--once` while
cross-checking the AlphaESS app's own fault/warning display, identify which words (if any) are
genuinely always-zero-when-healthy, and only then add a derived count — scoped to just those
words, not the whole block — back to `decode_fault_block`.

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
