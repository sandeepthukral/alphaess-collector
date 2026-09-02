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

**4. `tests/test_dispatch_goldens.py:330` string-sorts `plan_run`.**

```python
"runs": sorted(runs, key=lambda r: r["plan_run"]),
```

`plan.run_sort_key` exists precisely because these tags are not sortable as strings: runs
before 2026-07-30 carry `+02:00` where later ones carry `Z`. Use it.

**18. Every `*_HEARTBEAT_URL` still hard-codes an IP.** The second half of this item
shipped on 2026-09-02 and the first did not. `collector.send_heartbeat` now returns the
reason a ping failed instead of only logging it, the poll loop records that as a
`collector_health` point with `event="heartbeat_failed"`, the Collector Health dashboard has
a "Heartbeat unreachable" tile, and
`grafana/provisioning/alerting/alphaess-heartbeat-unreachable.yml` pages after ten minutes
of failures -- so "the watchdog is unreachable" is a visible state rather than something you
find by reading `docker logs`. A non-2xx reply counts as a failure too, which catches the
revoked-token case that previously looked entirely healthy from this side.

What remains is the cause rather than the detection: point every `*_HEARTBEAT_URL` at a DNS
or Tailscale name for the Kuma host so the next IP change costs nothing. The same stale
address lives in `EFFICIENCY_HEARTBEAT_URL`, `MIJNBATTERIJ_HEARTBEAT_URL` and all seven
dispatch URLs (`PLAN_INFLUX_`, `SLOTS_WRITTEN_`, `SLOTS_FRESH_`, `DISPATCHER_ALIVE_`,
`DISPATCH_CONFIRMED_`, `INVERTER_NOT_HIJACKED_`, `SOC_FLOOR_`), and compose reads env only at
container start, so every service needs a `--force-recreate`, not just the collector. Note
that the detection above covers ONLY the collector's own heartbeat: `efficiency.py`,
`mijnbatterij.py` and the seven dispatch monitors still fail silently, and are worth the same
treatment once the URLs stop moving. Two IP-change incidents so far, 2026-08-29 and the
2026-09-02 Kuma monitors still holding `192.168.68.105` in their own URL fields.

---

## Dispatch

**4b. Battery Plan's decode table has no pinned column order.** Flux does not promise one and
`union` of five `map`s does not produce one, so the columns arrive in whatever order the
engine felt like — the Dispatch dashboard rendered the same query with `Means` first, which
puts the thing you read last in front of the register you were looking for. Every other
generated table in the repo pins this with an `organize` transformation; that one does not.
Fixed on `alphaess-dispatch.json`, not yet on `alphaess-battery-plan.json`.

**5. Panel 8, "What to set in the app", should have gone at go-live — on Battery Plan.**
`DESIGN-dispatch.md` §7.4 makes deleting it a go-live step and explains why: it is the
dashboard face of `app_bands.py`, which §8 retires, so leaving it up puts two contradictory
instructions on one screen — a table telling you to type thresholds into the app, directly
above panels showing the dispatcher driving the same registers itself. §7.4 time-boxed that
overlap to the dry run. The dry run ended on 2026-08-18. Panel 7, "Planned Actions in app",
is the same class.

Both were on the **Overview** dashboard too, which neither §7.4 nor this item noticed, and
that pair is gone as of 2026-08-20. What remains is Battery Plan's.

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
(via `ha-alphaess-modbus`'s `all_registers.txt`), which corrects the handover's guessed
addresses by one register in three places. UPDATE 2026-09-02: a SECOND source disagrees with
that reading, and it is the more credible of the two. `senalse/ha-alphaess-modbus`'s
`custom_components/alphaess_modbus/const.py` addresses every 32-bit value at its FIRST word,
where the 2026-08-28 note read the manufacturer table as addressing it at its SECOND -- which
is precisely the one-register shift that produced all three "corrections". Per `const.py`:

| field | 2026-08-28 note | `const.py` |
|---|---|---|
| lifetime charge | `0x011F`-`0x0120` | `0x0120`-`0x0121` |
| lifetime discharge | `0x0121`-`0x0122` | `0x0122`-`0x0123` |
| lifetime grid-charge | `0x0123`-`0x0124` | `0x0124`-`0x0125` |
| inverter temperature | `0x0434` | `0x0435` |
| inverter lifetime PV | `0x043D`-`0x043E` | `0x043E`-`0x043F` |

`const.py`'s layout is gapless -- charge/discharge/grid-charge run `0x0120`-`0x0125` straight
into `0x0126`, which this repo has confirmed live as battery power -- where the 2026-08-28
reading has to posit an unused word at `0x0125` to fit. It is also self-consistent at the
inverter (temperature `0x0435`, warning1 `0x0436`), where the other reading would have the
warning word overlap the temperature. So the handover's original addresses were probably right
all along and the "correction" was the error. NOT ADOPTED ON THAT BASIS ALONE: "the tidier
document wins" is how `0x0883` came to be written down as the mode register.
`scripts/read-daily-health-registers.py` prints both alignments side by side from a live read,
and only one can produce three plausible, correctly ordered counters -- that is what settles
it. Run it (dispatch stopped, read-only) before anything here ships.

`const.py` also supplies the scales the earlier note did not: SoH `0x011B` is `int16` at
0.1 %/bit, every lifetime energy total is `uint32` at 0.1 kWh/bit, inverter temperature is
`int16` at 0.1 C/bit. Battery capacity `0x0119` at 0.1 kWh/bit gives 27.9 kWh, matching what
this repo already has independently from the plan, which is a free check that those scales are
being read correctly.

Two things remain unsourced. System lifetime PV energy at `0x08D1`-`0x08D2` appears in neither
`const.py` (which lists nothing between `0x08D0` and `0x08D4`, System Fault) nor anywhere else
found; treat it as unconfirmed regardless of what the probe shows. And checking the AlphaESS
app on 2026-08-28 found it surfaces no SoH%, no lifetime totals and no heatsink temperature at
all, so the cross-check path this item originally proposed does not exist for these four --
plausibility (bounds, ordering, monotonicity across two reads) is the whole of the evidence.

Once the alignment is settled: add a `DAILY_HEALTH_REFRESH_S` gate (~86400s) next to
`HEALTH_REFRESH_S`, and add the SoH/daily-energy/lifetime-cycle panels to
`alphaess-battery-health.json` in the SAME change -- it deliberately carries none today,
because a panel naming a field `dispatch/state.py` never writes is what
`test_every_field_filter_names_a_field_we_publish` exists to catch.

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
useless on a dashboard). `INVERTER_FW_BLOCK` (`0x0640`-`0x0653`) is ASCII version
strings, 2 chars/word: master firmware (`0x0640`-`0x0644`), slave firmware
(`0x0645`-`0x0649`), arm firmware (`0x064A`-`0x0653`) — live master and slave words are
byte-identical and decode to printable ASCII including a literal `.`. UPDATE 2026-09-02:
`senalse/ha-alphaess-modbus`'s `const.py` lists only TWO strings here, inverter version
(`0x0640`, 5 words) and inverter **ARM** version (`0x0645`, 5 words), with nothing at
`0x064A`. The byte-identical observation that made the three-string reading look right is
equally well explained by there being no slave string at all and `0x0645` holding the ARM
version. Decode the two `const.py` names and leave `0x064A`-`0x0653` raw until something
confirms what is in it. Decode the numeric/enum fields with
confidence; leave battery-ready and system-mode's human-readable labels either raw or
clearly caveated, since neither has an independent confirmation source. No gate/cadence
change needed, just extending `decode_firmware_block`/`decode_inverter_fw_block`/
`decode_system_config_block` in `registers.py`.

**15. The fault block is read once an hour, with no latching, so a fault that raises and
clears inside that hour is never observed at all.** The rest of this item shipped on
2026-09-02: `FAULT_BLOCK` is now the full 24 words (`0x0131`-`0x0148`, twelve 32-bit values,
fault1-6 then warning1-6 -- it was 22, stopping one 32-bit value short, so warning6 was never
read), `decode_fault_block` derives `active_fault_count`/`active_warning_count` as popcounts,
and `alphaess-battery-health.json` leads with both as row-1 stats. What did not ship is a
decision on cadence, because it is a real trade rather than an oversight. The hourly
`HEALTH_REFRESH_S` gate (`scheduler.py` step 8c) was the handover's own choice, made when
nobody knew what these words held; now that the block's shape is confirmed, the question is
whether a genuinely fault-carrying word should be sampled every tick like the temp block, or
latched sticky ("seen nonzero since the last hourly publish"), rather than read only at the
instant the gate fires. Note that latching without sampling more often buys nothing -- a
sticky flag can only latch what it saw -- so the two options are really "read every tick and
publish the count" versus "read every tick, publish the count AND a since-last-publish high
water mark". Both cost a Modbus round-trip every tick, which step 8c's own comment is why the
gate exists. Worth resolving against how often the count is actually nonzero: it has read zero
every time it has been looked at, so there is no evidence yet that transient faults happen
here at all. Live values across the full 24 words were all zero on 2026-08-28 (22 words) and
warning6 has still never been observed.

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
