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

**13. DONE 2026-09-03 — the daily tier ships: SoH, lifetime charge/discharge/grid-charge,
lifetime PV, inverter heatsink.** Kept here rather than deleted because the ADDRESSES are the
finding, and the next person to read a register document for this inverter needs the outcome.

Two documentary sources disagreed by exactly one register.
`senalse/ha-alphaess-modbus`'s `const.py` addresses a 32-bit value at its FIRST word; this
repo's 2026-08-28 reading of AlphaESS's own parameter table put it at the SECOND, which is what
generated all three of that note's "corrections". `scripts/read-daily-health-registers.py` read
both alignments side by side off the live inverter and const.py won outright:

| field | rejected (2026-08-28) | CONFIRMED live |
|---|---|---|
| lifetime charge | `0x011F` → 0 | `0x0120`-`0x0121` → 1048.1 kWh |
| lifetime discharge | `0x0121` → 686,882,816 | `0x0122`-`0x0123` → 1022.1 kWh |
| lifetime grid-charge | `0x0123` → 669,843,456 | `0x0124`-`0x0125` → 581.1 kWh |
| inverter heatsink | `0x0434` → 0 | `0x0435` → 37.0 C |
| SoH | `0x011B` (both agreed) | `0x011B` → 100.0 % |
| lifetime PV | `0x08D1` → 1,387,724,801 | `0x08D0`-`0x08D1` → 867.11 kWh |

The rejected column is not just wrong, it is wrong diagnostically: those two nine-digit numbers
are one counter's low word spliced onto the next one's high word. So the handover's ORIGINAL
addresses were right all along and the 2026-08-28 "correction" was itself the error — the same
failure mode as `0x0883`, and caught the same way, by refusing to ship a documented address
until the hardware agreed.

The scale (0.1 kWh/bit, 0.1 %/bit, 0.1 C/bit) is confirmed twice over and independently of the
addresses: battery capacity `0x0119` read 279 against a battery this repo knows from the planner
is 27.9 kWh, and lifetime PV read 8671.1 kWh, right for this array where 1 kWh/bit would claim
86 MWh.

UPDATE, SAME DAY, AFTER DEPLOYING: the first day of published values caught a wrong scale, and
it was the field the item itself flagged as least confirmed. **Lifetime PV is 0.01 kWh/bit, not
the 0.1 the battery counters use — 867.1 kWh, not 8671.1.** Three independent disproofs, none of
which the probe could have produced:

- the register moved 18.9 kWh in 2 h 24 min: a 7.9 kW average through a 5 kW inverter
- `power_readings.pv_power_w` integrated **1.98 kWh** over the identical window — the same
  delta at 0.01
- `daily_energy.pv_kwh_api` totals **835 kWh across the collector's entire 46-day history**,
  against a claimed lifetime of 8671 that would need ~500 days of it

It shipped at 0.1 because 8671 kWh "looked right for an array of this age". That is not a
measurement, and the tier's own comments said as much about everything else while this one got
a pass. THE LESSON IS ABOUT THE METHOD, not the register: a lifetime counter is confirmed by
comparing its DELTA against a meter that measured the same energy, and this repo has such a
meter for PV and for battery charge. The battery counters passed that same test the same day
(+0.9–1.8 kWh published against 1.27 kWh integrated), so 0.1 is confirmed for them by
measurement rather than by the capacity register alone.

Two things the probe could not settle, both now closed by the deployed fields:

- **Monotonicity.** Nothing moved across the probe's 180 s gap — at 1479 W the battery makes
  74 Wh, under the resolution. The published series settled it within hours: every counter
  rose, by amounts the site can produce, and the heatsink tracked the afternoon.
- **`0x08D2` held the same value as `0x08D0`.** Only the first is published, and they are
  deliberately not required to agree — see `registers.DAILY_PV_BLOCK`.

Still open: the round trip reads 97.5%, above the 90–96% an AC round trip would show. Consistent
with DC-side counters, still unproven, and deliberately not encoded in any guard.

The inverter's own lifetime PV (`0x043D`-`0x043F`) is NOT published and should not be added:
the whole `0x0430`-`0x0440` window read zero except the heatsink. Not an alignment question,
both candidates read 0 — this is an AC-coupled site behind APsystems micro-inverters (see
`REG_PV_METER`) and the inverter has no strings of its own to count.

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
