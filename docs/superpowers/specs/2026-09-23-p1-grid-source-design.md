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
see `dispatch/heartbeat.py`'s docstring). The fetch is wrapped in the same
`try`/`except OSError` block that already handles a bad Modbus read: on failure,
`surplus_w = None` and `batt_w = None`, which is the existing fail-safe (a met charge target
holds instead of releasing; see `DISPATCH-FLOW.md`'s box C). No new failure path here
either.

`batt_w` continues to come from `REG_BATTERY_POWER` regardless of `GRID_SOURCE` — the
battery reading is unaffected by the phase mismatch.

### Docs

`docs/DISPATCH-FLOW.md` box C ("read live SoC / grid_w / battery_w → surplus_w") gets a note
that `grid_w`'s source is gated by `GRID_SOURCE`, with the P1-unreachable case folding into
the existing "implausible/failed read → surplus_w=None" path already drawn in the flowchart
— no new branch shape, just a note on where `grid_w` comes from.

`docs/EFFICIENCY-FLOW.md` and `docs/PRICING-FLOW.md` treat `power_readings` as a given input
and don't diagram `collector.py`'s field mapping, so they need no changes — confirmed during
exploration, re-checked once the collector.py change lands in case that's stopped being true.

### Testing

- `collector/collector.py`: unit test that `GRID_SOURCE=p1` overrides `grid_power_w` and
  recomputes `load_power_w` per the identity above; a P1 fetch failure surfaces as a poll
  failure indistinguishable from an AlphaESS fetch failure (reuses
  `tests/test_collector_failure_domains.py`'s pattern).
- `dispatch/scheduler.py`: unit test that `GRID_SOURCE=p1` sources `grid_w` from the P1 fetch
  instead of `inv.read(REG_GRID_POWER, ...)`, and that a P1 fetch failure produces the same
  `surplus_w=None`/`batt_w=None` outcome as today's bad-Modbus-read path
  (`tests/test_dispatch_scheduler.py`).
- Both: `GRID_SOURCE=inverter` (or unset) reproduces exactly today's behavior — existing
  tests for both modules should pass unmodified under the default.
