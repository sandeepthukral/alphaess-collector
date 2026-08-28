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

**13. The health-poller's daily tier (SoH, lifetime energy, heatsink temp, PV energy) has no
confirmed register layout.** UPDATE 2026-08-28: found AlphaESS's own "Parameter address table"
(via `ha-alphaess-modbus`'s `all_registers.txt`), and it corrects the handover's guessed
addresses by one register in three places — the same class of off-by-one #12 turned out to be
before it was resolved. Per that table: SoH is `0x011B` (unsigned, 0.1%/bit — matches the
handover). Lifetime charge/discharge/grid-charge energy is a 3×2-word block starting at
**`0x011F`**, not `0x0120` — charge (`0x011F`-`0x0120`), discharge (`0x0121`-`0x0122`),
grid-charge (`0x0123`-`0x0124`); `0x0125` is a gap, then `0x0126` is the already-confirmed
`REG_BATTERY_POWER`, which is what pins this boundary. Inverter heatsink temp ("INV
Temperature") is **`0x0434`**, not `0x0435` — `0x0435`-`0x0436` is actually the start of the
inverter's own fault/warning words. PV energy is two 2-word fields, not two 1-word ones:
inverter lifetime PV energy is **`0x043D`-`0x043E`**, system lifetime PV energy is
**`0x08D1`-`0x08D2`**. Still needs live-inverter verification before shipping, per this
module's own rule — checked what the AlphaESS app shows on 2026-08-28 and it does not surface
SoH%, lifetime energy totals, or a heatsink temperature reading at all, so the cross-check
path this item originally proposed doesn't exist for these four; a `--once` read is still the
right move, just checked for plausibility (bounds, monotonicity across two reads) rather than
against an app number. Once confirmed, add a `DAILY_HEALTH_REFRESH_S` gate (`~86400s`) next to
`HEALTH_REFRESH_S`. `alphaess-battery-health.json` carries no SoH/daily-energy/lifetime-cycle
panels until these fields exist, so add both in one change.

**14. The health-poller's weekly firmware/system-config blocks are published as raw hex, not
decoded fields.** UPDATE 2026-08-28: the same manufacturer table resolves the word-by-word
layout of all three blocks, cross-checked against live raw values already in `dispatch_state`.
`FIRMWARE_BLOCK` (`0x0115`-`0x011A`) is BMU firmware version, LMU firmware version, ISO
firmware version, battery pack count, battery capacity, battery type, one word each — live
values on 2026-08-28 read pack count `3` (matches the already-live-confirmed "three-pack site"
note in `registers.py`) and capacity `279` → ×100 = 27,900 Wh, matching the capacity this repo
already has independently from the plan (`DESIGN-dispatch.md`). `SYSTEM_CONFIG_BLOCK`
(`0x0800`-`0x080F`) is max feed-into-grid % (`0x0800`, 1%/bit), PV capacity storage-side
(`0x0801`-`0x0802`, 1W/bit), PV capacity grid-inverter-side (`0x0803`-`0x0804`, 1W/bit —
live reads `5000`, matching the site's known 5 kW inverter), system mode (`0x0805`,
1=AC/2=DC/3=Hybrid), meter CT select (`0x0806`), battery-ready flag (`0x0807`, 0=OFF/1=ON —
live reads `0`, which does not obviously match a battery that is plainly in use; the AlphaESS
app checked 2026-08-28 has no explicit "battery ready" indicator, only charge/discharge
activity, so this one flag's meaning stays unconfirmed even though its address is now solid),
IP method (`0x0808`), local IP/subnet/gateway/Modbus address (`0x0809`-`0x080F`, mostly
useless on a dashboard). `INVERTER_FW_BLOCK` (`0x0640`-`0x0653`) is three ASCII version
strings, 2 chars/word: inverter master firmware (`0x0640`-`0x0644`, 5 words), inverter slave
firmware (`0x0645`-`0x0649`, 5 words), inverter arm firmware (`0x064A`-`0x0653`, 10 words) —
live master and slave words are byte-identical and decode to printable ASCII including a
literal `.`, which is consistent with this being right. Decode the numeric/enum fields with
confidence; leave battery-ready and system-mode's human-readable labels either raw or
clearly caveated, since neither has an independent confirmation source. No gate/cadence
change needed, just extending `decode_firmware_block`/`decode_inverter_fw_block`/
`decode_system_config_block` in `registers.py`.

**15. `FAULT_BLOCK` (`0x0131`-`0x0146`) has no derived "how many faults are active" summary,
and needs one confirmed live before it gets one.** Raised in PR #129's review: a naive
`sum(1 for w in words if w != 0)` looked safe because it needs no bit-level knowledge to
compute, but the range itself was not confirmed to be fault/warning bits exclusively — if even
one word in it turned out to be a normally-nonzero status value, counter, or nameplate figure
rather than a fault flag, that count would pin itself at 1 or more permanently, which reads on
the health dashboard as "faults active" forever and is worse than publishing no summary at
all, because it looks confident. UPDATE 2026-08-28: better news than feared. AlphaESS's own
"Parameter address table" (see item 13/14's update, same source) shows `0x0131`-`0x0148` is
*exclusively* six 32-bit fault words and six 32-bit warning words — fault1-6 then warning1-6,
nothing else interleaved — so a nonzero-word count is structurally safe once the block is
sized correctly. It currently is not: `FAULT_BLOCK` is 22 words (`0x0131`-`0x0146`), which
covers fault1-6 and warning1-5 but stops one warning short of the full block — extend it to 24
words (`0x0131`-`0x0148`) to pick up warning6 before adding the count. Live values queried
2026-08-28 read all zero across the current 22 words, consistent with "no active faults" and a
readable, working block. Add the derived `active_fault_count` (a straight nonzero-word count
over the full corrected block, no scoping needed now that every word's role is confirmed) and
an "Active faults" stat to `alphaess-battery-health.json` row 1 in the same change — today that
row has no fault summary tile at all.

Also raised in the same review, and worth deciding alongside this rather than separately: the
block is sampled once an hour (`HEALTH_REFRESH_S`, `scheduler.py` step 8c), with no latching,
so a fault that raises and clears again inside that hour is never observed at all -- the
dashboard would show clean the whole time. The hourly cadence itself was the handover's own
choice, not something to second-guess before the block's contents are even confirmed, but once
this item and item 14 settle what these words actually mean, it's worth asking then whether a
genuinely fault-carrying word should be sampled every tick (like the temp block) or latched
sticky ("seen nonzero since last hourly publish") rather than read only at the instant the gate
fires.

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
