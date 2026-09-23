# P1 monitor as grid/load source (feature-flagged)

## Problem

The house wiring was upgraded from 1-phase to 3-phase; the AlphaESS inverter is still
1-phase and has not been replaced by the vendor. Its Modbus grid-power register and its
cloud API's `pgrid`/`pload` fields only ever saw one phase, so they no longer reflect real
grid import/export or house load — only PV and battery power (which the inverter measures
directly, not via phase CTs) are still correct.

A HomeWizard-compatible P1 monitor is already installed and reachable on the LAN
(`http://192.168.2.46/api/v1/data`) and reports true whole-house grid power across all three
phases (`active_power_w`, positive = import — same sign convention AlphaESS uses for
`pgrid`).

This must be reversible: once the vendor replaces the inverter, operation must return to
using the inverter's own readings without code changes, only a config flip.

## Non-goals

- No change to `slots.decide()`/`clamp()`'s branching, thresholds, or `efficiency.py`/
  `pricing.py`'s `gate()`/`compute_day()`/`audit_day()` logic. Only the *inputs* to these
  change; the decision logic itself is untouched.
- No attempt to read a "load" field from the P1 API. It doesn't expose one, and the load
  balance identity (below) derives it without needing to.
- No change to how PV or battery power are read — both remain correct from the inverter.

## Background: the load identity

Verified on this repo's own data (`power_readings`, 56,969/56,969 samples, exact to the
watt): `load_power_w == pv_power_w + grid_power_w + battery_power_w`. `load_power_w` has
never been an independent measurement — the AlphaESS API derives it as a residual. This
means correcting `grid_power_w` alone is sufficient to correct `load_power_w` too, by
recomputing the same identity with the corrected grid value.

## Design

### Config

New env var `GRID_SOURCE`, values `p1` | `inverter`, default `inverter`. When `p1`,
`P1_MONITOR_URL` (e.g. `http://192.168.2.46/api/v1/data`) must be set — both collector and
dispatch containers already reach LAN devices (dispatch already speaks Modbus TCP to the
inverter's LAN IP). Read via each module's existing config convention: `env()` helper in
`collector.py`; in `scheduler.py`, `tick()` takes no config args (`inv, slots_path, cache,
now`) and argparse's `Namespace` never reaches it, so `GRID_SOURCE`/`P1_MONITOR_URL` are read
as module-level `os.environ.get(...)` globals at import time, the same pattern as
`HEARTBEAT_PATH` and `MONITOR_URLS`.

`scheduler.py`'s `MONITOR_URLS` dict also gains `"p1-reachable": os.environ.get("P1_REACHABLE_HEARTBEAT_URL", "")` — see the new monitor below.

Reverting: set `GRID_SOURCE=inverter` (or unset it). No code path is removed — this is
purely a gate — so rollback is a deploy with a changed `.env`, not a revert commit.

### `collector/collector.py` (historical recording, feeds `efficiency.py`/`pricing.py`)

Inside the existing poll's `try` block, when `GRID_SOURCE=p1`: after the AlphaESS fetch,
also fetch `P1_MONITOR_URL` via `requests` (already imported). `parse_fields(data: dict)`
only takes the AlphaESS response and has no second parameter today, so it gains one:
`parse_fields(data: dict, p1_data: dict | None = None)`. When `p1_data` is given, it
overrides `grid_power_w` with `p1_data["active_power_w"]` and recomputes
`load_power_w = pv_power_w + grid_power_w + battery_power_w` using the corrected grid value
and the inverter's own (still-correct) `pv_power_w`/`battery_power_w`. The call site
(`collector.py:578`, `fields = parse_fields(data)`) passes `p1_data` only when
`GRID_SOURCE=p1`.

The P1 fetch shares the poll's existing `try`/`except` and `stage` tracking (`collector.py`
around line 573), so a P1 failure is handled by the exact machinery that already exists for
an AlphaESS API failure: it counts as a consecutive failure, drives the same backoff,
`collector_health` event, and Kuma heartbeat message — no new failure path, and the poll
writes nothing rather than writing a silently-wrong single-phase number.

### `dispatch/scheduler.py` (live dispatch, feeds `slots.decide()`'s surplus-harvest)

In `tick()`, when `GRID_SOURCE=p1`: replace the `inv.read(R.REG_GRID_POWER, 2, signed=True)`
call with a P1 fetch, using `urllib.request` (this image deliberately has no `requests` —
see `dispatch/heartbeat.py`'s docstring), with an explicit timeout (matching every other
network call in this repo, e.g. `heartbeat.py`'s 5s default, `collector.py`'s 5-30s range —
`urlopen()` with no timeout can hang indefinitely on a socket that connects but never
responds, stalling the 60s control loop past its watchdogs).

`tick()` is async, and every other network call inside it is already async or offloaded
(`inv.read` via pymodbus's async client, heartbeats via `asyncio.to_thread` — see
`report()`, `scheduler.py:481-482`); a raw blocking `urlopen()` call would stall the whole
event loop, including heartbeats, for the fetch's duration. The P1 fetch is wrapped in
`asyncio.to_thread` the same way.

The fetch is wrapped in a `try`/`except` alongside the existing block that already handles a
bad Modbus read. That block currently catches only `OSError`, which covers a connection
failure but not a malformed response — a non-JSON body or one missing `active_power_w`
raises `JSONDecodeError`/`KeyError`, neither an `OSError`, and would otherwise crash `tick()`
instead of degrading gracefully. The except clause is broadened (or an explicit second
`except` added) to cover those too. Any of these failures — connection, timeout, malformed
response — produces the same `surplus_w = None`/`batt_w = None` outcome as today's bad
Modbus read: the existing fail-safe (a met charge target holds instead of releasing; see
`DISPATCH-FLOW.md`'s box C). No new failure *behavior*, just a wider net catching it.

`batt_w` continues to come from `REG_BATTERY_POWER` regardless of `GRID_SOURCE` — the
battery reading is unaffected by the phase mismatch.

### New Kuma monitor: `p1-reachable`

Today, a P1 fetch failure on the dispatch side degrades silently: `surplus_w = None` and a
`log.warning`, same as a bad Modbus read has always done, with no Kuma signal either way. For
the *existing* Modbus register that's an accepted gap (`DESIGN-dispatch.md` §6.1 has no
monitor for it either). It is not acceptable to carry the same silence over to a **new**
external dependency this change is deliberately introducing as the primary data source —
`DESIGN-dispatch.md` §6.1's entire premise is that a silent degradation gets a monitor, one
per way of being silently wrong.

Add monitor `p1-reachable`, pinged by the dispatcher every tick, alongside the existing five
(`monitor_pings()`, `scheduler.py:404`): `up` when the P1 fetch this tick succeeded, `down`
with the exception summary when it didn't. Only pinged when `GRID_SOURCE=p1` — like `#8
soc-floor`'s "not pinged when not applicable" rule, pinging `down` under `GRID_SOURCE=inverter`
would alarm on a monitor nobody configured a URL for. `monitor_pings()` is pure and decided
from one tick's facts (its own docstring's reason for existing), so it takes the P1 fetch
outcome as a new parameter rather than reading global state.

This becomes monitor **#10** in `DESIGN-dispatch.md` §6.1's table: "Loop alive, but the
battery's surplus-harvest decisions are blind — grid reads have silently fallen back to
freeze." Cadence matches #7/#8 (5-15 min grace — a single dropped P1 read is not an outage,
a sustained one is).

### Docs

`docs/DISPATCH-FLOW.md` box C ("read live SoC / grid_w / battery_w → surplus_w") gets a note
that `grid_w`'s source is gated by `GRID_SOURCE`, with the P1-unreachable case folding into
the existing "implausible/failed read → surplus_w=None" path already drawn in the flowchart
— no new branch shape, just a note on where `grid_w` comes from.

`DESIGN-dispatch.md` §6.1's monitor table gets the new `p1-reachable` row (#10), plus a line
in the "which monitor catches what" narrative alongside #7/#8 (`docs/DESIGN-dispatch.md` is
not in `CLAUDE.md`'s sync table, since it documents narrative/monitors rather than
branching/thresholds, but it is the source of truth for the monitor list and would go stale
otherwise).

`docs/EFFICIENCY-FLOW.md` and `docs/PRICING-FLOW.md` treat `power_readings` as a given input
and don't diagram `collector.py`'s field mapping, so they need no changes — confirmed during
exploration, re-checked once the collector.py change lands in case that's stopped being true.

### Testing

- `collector/collector.py`: unit test that `GRID_SOURCE=p1` overrides `grid_power_w` and
  recomputes `load_power_w` per the identity above; a P1 fetch failure surfaces as a poll
  failure indistinguishable from an AlphaESS fetch failure (reuses
  `tests/test_collector_failure_domains.py`'s pattern).
- `dispatch/scheduler.py`: unit test that `GRID_SOURCE=p1` sources `grid_w` from the P1 fetch
  instead of `inv.read(REG_GRID_POWER, ...)`, and that a connection failure, a timeout, and a
  malformed response (missing `active_power_w`) each produce the same
  `surplus_w=None`/`batt_w=None` outcome as today's bad-Modbus-read path
  (`tests/test_dispatch_scheduler.py`). Also verify the fetch runs off the event loop (e.g.
  that a slow/blocking P1 response doesn't delay a concurrent heartbeat in the test).
- `monitor_pings()`: unit test that `p1-reachable` pings `up` on a successful P1 fetch,
  `down` with the failure reason on an unsuccessful one, and is absent from the ping list
  entirely under `GRID_SOURCE=inverter`.
- Both: `GRID_SOURCE=inverter` (or unset) reproduces exactly today's behavior — existing
  tests for both modules should pass unmodified under the default.
