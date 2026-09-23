# P1 Grid Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a HomeWizard-compatible P1 monitor stand in for the AlphaESS inverter's grid-power reading, in both live dispatch and historical recording, gated by a `GRID_SOURCE` flag that reverts to today's behavior with one config change.

**Architecture:** Two independent call sites read grid power today — `dispatch/scheduler.py`'s `tick()` (Modbus register, live surplus-harvest decision) and `collector/collector.py`'s poll loop (AlphaESS cloud API, historical InfluxDB recording). Each gains its own P1 fetch, gated by the same `GRID_SOURCE` env var, feeding into the existing computation (`surplus_w` / `load_power_w`) rather than replacing it. A P1 failure is folded into each module's existing failure-handling path — no new failure *behavior*, except one new addition: a Kuma monitor for the dispatch side, because that side had no visibility into a failed grid read at all before this change.

**Tech Stack:** Python 3.14, `requests` (collector image), stdlib `urllib.request` (dispatch image — no `requests` there by design), `asyncio.to_thread` (dispatch image is async).

**Spec:** `docs/superpowers/specs/2026-09-23-p1-grid-source-design.md`

## Global Constraints

- `GRID_SOURCE` env var: `p1` | `inverter`, default `inverter`. Read via each module's existing config convention (see Task 1 and Task 3).
- `P1_MONITOR_URL` env var: full URL to the P1 monitor's `/api/v1/data` endpoint (e.g. `http://192.168.2.46/api/v1/data`). Required when `GRID_SOURCE=p1`.
- P1's `active_power_w` field is positive-on-import — same sign convention as AlphaESS's `pgrid` and `REG_GRID_POWER`. No sign flip anywhere in this plan.
- `load_power_w = pv_power_w + grid_power_w + battery_power_w` (verified exact identity — see spec background). Never read "load" from P1; always recompute via this identity using the corrected `grid_power_w`.
- On any P1 failure (unreachable, timeout, malformed response), never fall back to the inverter's reading. Treat it exactly as the existing failure path for that module already treats a bad read.
- Every network call gets an explicit timeout. No exceptions.

---

### Task 1: `collector/collector.py` — P1 fetch helper and `parse_fields` override

**Files:**
- Modify: `collector/collector.py` (`parse_fields`, ~line 496; add `fetch_p1_data` near `get_last_power_data`, ~line 299)
- Test: `tests/test_collector_helpers.py`

**Interfaces:**
- Produces: `fetch_p1_data(url: str, timeout: float = 10) -> dict` — raises `RuntimeError` on transport errors or a response missing `active_power_w`.
- Produces: `parse_fields(data: dict, p1_data: dict | None = None) -> dict` — unchanged when `p1_data` is `None`; when given, overrides `grid_power_w` and recomputes `load_power_w`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_collector_helpers.py`, after the existing `parse_fields` tests (after `test_parse_fields_on_an_empty_response`, ~line 140):

```python
def test_parse_fields_overrides_grid_and_recomputes_load_from_p1():
    fields = parse_fields(
        {"ppv": 1500, "pgrid": -9999, "pload": -9999, "pbat": -500, "soc": 87.5},
        p1_data={"active_power_w": 200},
    )
    assert fields["grid_power_w"] == 200.0
    # load = pv + grid + battery = 1500 + 200 + (-500)
    assert fields["load_power_w"] == 1200.0
    assert fields["pv_power_w"] == 1500.0
    assert fields["battery_power_w"] == -500.0


def test_parse_fields_ignores_p1_data_when_none():
    fields = parse_fields({"ppv": 1500, "pgrid": -200, "pload": 800,
                           "pbat": -500, "soc": 87.5}, p1_data=None)
    assert fields["grid_power_w"] == -200.0
    assert fields["load_power_w"] == 800.0
```

Add a new section at the end of `tests/test_collector_helpers.py`:

```python
# --------------------------------------------------------------------------
# fetch_p1_data
# --------------------------------------------------------------------------

def test_fetch_p1_data_returns_the_body(monkeypatch):
    import collector as collector_mod

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"active_power_w": 8742, "active_power_l1_w": 3640}

    monkeypatch.setattr(collector_mod.requests, "get",
                        lambda url, timeout=10: FakeResponse())
    body = collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data")
    assert body["active_power_w"] == 8742


def test_fetch_p1_data_raises_on_missing_active_power_w(monkeypatch):
    import collector as collector_mod

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"active_power_l1_w": 3640}

    monkeypatch.setattr(collector_mod.requests, "get",
                        lambda url, timeout=10: FakeResponse())
    with pytest.raises(RuntimeError, match="active_power_w"):
        collector_mod.fetch_p1_data("http://192.168.2.46/api/v1/data")
```

Check the top of `tests/test_collector_helpers.py` for its existing import style (`import pytest`, `from collector import ...` or `import collector`) and match it — do not introduce a second import style in the same file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_collector_helpers.py -v -k "p1"`
Expected: FAIL — `parse_fields() got an unexpected keyword argument 'p1_data'` and `module 'collector' has no attribute 'fetch_p1_data'`.

- [ ] **Step 3: Implement `fetch_p1_data`**

In `collector/collector.py`, add after `get_last_power_data` (after line 299, before `format_duration`):

```python
def fetch_p1_data(url: str, timeout: float = 10) -> dict:
    """Fetch a live snapshot from a HomeWizard-compatible P1 monitor's local API.

    Raises RuntimeError on transport errors or a response missing active_power_w --
    the one field this collector uses. Positive active_power_w = importing from the
    grid, same convention as AlphaESS's pgrid.
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if "active_power_w" not in body:
        raise RuntimeError(f"P1 response missing active_power_w: {body}")
    return body
```

- [ ] **Step 4: Implement the `parse_fields` override**

Replace `parse_fields` (lines 496-512) with:

```python
def parse_fields(data: dict, p1_data: dict | None = None) -> dict:
    """Extract the fields we store. All powers in watts.

    Sign conventions (per AlphaESS API):
      pgrid: positive = importing from grid, negative = exporting
      pbat:  positive = discharging battery, negative = charging
    Verify against a live response with --once before trusting dashboards.

    `p1_data`, when given (GRID_SOURCE=p1), overrides grid_power_w with the P1
    monitor's active_power_w -- same sign convention as pgrid -- and recomputes
    load_power_w from the load identity (load = pv + grid + battery), since
    load_power_w has never been an independent measurement: it is AlphaESS's own
    residual, wrong in exactly the way grid_power_w is wrong on a phase-mismatched
    inverter, and right again once grid_power_w is corrected.
    """
    fields = {
        "pv_power_w": data.get("ppv"),
        "grid_power_w": data.get("pgrid"),
        "load_power_w": data.get("pload"),
        "battery_power_w": data.get("pbat"),
        "soc_percent": data.get("soc"),
    }
    missing = [k for k, v in fields.items() if v is None]
    if missing:
        log.warning("API response missing fields: %s (raw keys: %s)",
                    missing, sorted(data.keys()))
    fields = {k: float(v) for k, v in fields.items() if v is not None}
    if p1_data is not None:
        fields["grid_power_w"] = float(p1_data["active_power_w"])
        if "pv_power_w" in fields and "battery_power_w" in fields:
            fields["load_power_w"] = (
                fields["pv_power_w"] + fields["grid_power_w"] + fields["battery_power_w"])
    return fields
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_collector_helpers.py -v -k "p1"`
Expected: PASS (all 4 new tests)

Run: `pytest tests/test_collector_helpers.py -v`
Expected: PASS (no regressions in the existing `parse_fields` tests)

- [ ] **Step 6: Commit**

```bash
git add collector/collector.py tests/test_collector_helpers.py
git commit -m "Add P1 fetch helper and grid/load override to parse_fields"
```

---

### Task 2: `collector/collector.py` — wire P1 fetch into `run_loop`

**Files:**
- Modify: `collector/collector.py` (`run_loop`, ~line 517-578)
- Test: `tests/test_collector_failure_domains.py`

**Interfaces:**
- Consumes: `fetch_p1_data(url, timeout)`, `parse_fields(data, p1_data)` from Task 1.

**Doubt to flag if it comes up:** `run_loop` reads its other optional config (like `heartbeat_url`) via `os.environ.get(...)` directly rather than the `env()` helper, because `env()` treats an unset var as fatal only when no default is given — `GRID_SOURCE` and `P1_MONITOR_URL` follow that same "optional with a default" shape, so use `os.environ.get`, not `env()`, for consistency with `heartbeat_url`'s line right above where you'll add this.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_collector_failure_domains.py`, using the same `harness` fixture and `loop_env` pattern already in that file (read the whole file first — the `run(*, fetch, ...)` helper takes a `fetch(poll_number)` callback and drives `run_loop` for `stop_after` polls). Add a new test class at the end:

```python
class TestP1GridSource:
    """GRID_SOURCE=p1 folds a P1 fetch into the same poll, sharing its failure domain."""

    def test_p1_data_overrides_grid_and_load_in_the_written_point(
            self, monkeypatch, harness):
        monkeypatch.setenv("GRID_SOURCE", "p1")
        monkeypatch.setenv("P1_MONITOR_URL", "http://192.168.2.46/api/v1/data")
        monkeypatch.setattr(
            collector, "fetch_p1_data",
            lambda url, timeout=10: {"active_power_w": 300})

        def fetch(poll):
            return {"ppv": 1000, "pgrid": -9999, "pload": -9999,
                    "pbat": -200, "soc": 80}

        state = harness(fetch=fetch, stop_after=1)
        point = state["write_api"].points[0]
        line = point.to_line_protocol()
        assert "grid_power_w=300" in line
        assert "load_power_w=1100" in line  # 1000 + 300 + (-200)

    def test_a_p1_fetch_failure_counts_as_a_poll_failure(self, monkeypatch, harness):
        """Same failure domain as an AlphaESS API failure -- no separate handling."""
        monkeypatch.setenv("GRID_SOURCE", "p1")
        monkeypatch.setenv("P1_MONITOR_URL", "http://192.168.2.46/api/v1/data")

        def boom(url, timeout=10):
            raise RuntimeError("P1 unreachable")

        monkeypatch.setattr(collector, "fetch_p1_data", boom)

        def fetch(poll):
            return {"ppv": 1000, "pgrid": -100, "pload": 900, "pbat": -200, "soc": 80}

        state = harness(fetch=fetch, stop_after=1)
        assert state["write_api"].points == []
        assert any(e["event"] == "failure" for e in state["health_events"])

    def test_grid_source_inverter_default_never_calls_fetch_p1_data(
            self, monkeypatch, harness):
        called = []
        monkeypatch.setattr(collector, "fetch_p1_data",
                            lambda url, timeout=10: called.append(1))

        def fetch(poll):
            return {"ppv": 1000, "pgrid": -100, "pload": 900, "pbat": -200, "soc": 80}

        harness(fetch=fetch, stop_after=1)
        assert called == []
```

`state["write_api"].points` (a list of the raw `Point` objects passed to `write_api.write(record=...)`) and `state["health_events"]` (each a dict with an `"event"` key, among others) are `harness`'s own accessors, defined in `tests/test_collector_failure_domains.py`'s `FakeWriteApi` and `fake_health_event` — read that file's top (shown in this task's exploration) before writing these tests. `Point.to_line_protocol()` is confirmed to render `power_readings,sys_sn=x grid_power_w=300,load_power_w=1100` for integer-valued floats (verified directly against the installed `influxdb_client` package) — use it as shown, no `float()`-suffix `.0` in the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_collector_failure_domains.py -v -k "P1GridSource"`
Expected: FAIL — `run_loop` doesn't read `GRID_SOURCE` yet, so `fetch_p1_data` is never called and the override never happens.

- [ ] **Step 3: Wire it into `run_loop`**

In `collector/collector.py`, find where `heartbeat_url` is read (around line 541: `heartbeat_url = os.environ.get("HEARTBEAT_URL", "")`) and add right after it:

```python
    grid_source = os.environ.get("GRID_SOURCE", "inverter")
    p1_monitor_url = os.environ.get("P1_MONITOR_URL", "")
    if grid_source == "p1" and not p1_monitor_url:
        log.error("GRID_SOURCE=p1 requires P1_MONITOR_URL")
        sys.exit(1)
```

Then in the poll loop, find the `stage = "fetch"` block (around line 575):

```python
        stage = "fetch"
        try:
            data = get_last_power_data(app_id, app_secret, sys_sn)
            fields = parse_fields(data)
```

Replace those three lines with:

```python
        stage = "fetch"
        try:
            data = get_last_power_data(app_id, app_secret, sys_sn)
            p1_data = fetch_p1_data(p1_monitor_url) if grid_source == "p1" else None
            fields = parse_fields(data, p1_data)
```

Everything below (the `if fields:` block, the `except Exception as exc:` handler) is unchanged — a `fetch_p1_data` failure raises inside the same `try`, hits the same `except Exception as exc:` a few lines down, and is indistinguishable from an AlphaESS fetch failure to every downstream consumer (backoff, `collector_health`, the Kuma heartbeat message).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_collector_failure_domains.py -v`
Expected: PASS (all tests in the file, including the 3 new ones and the existing ones unmodified)

- [ ] **Step 5: Run the full collector test suite**

Run: `pytest tests/test_collector_helpers.py tests/test_collector_failure_domains.py tests/test_collector_heartbeat.py tests/test_collector_backoff.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add collector/collector.py tests/test_collector_failure_domains.py
git commit -m "Wire GRID_SOURCE=p1 into the collector poll loop"
```

---

### Task 3: `dispatch/scheduler.py` — P1 fetch helper and `tick()` wiring

**Files:**
- Modify: `dispatch/scheduler.py` (imports; module-level config near `MONITOR_URLS`, ~line 60; `tick()`'s surplus block, ~line 647-663)
- Test: `tests/test_dispatch_scheduler.py`

**Interfaces:**
- Produces: `fetch_p1_grid_w(url: str, timeout: float = P1_FETCH_TIMEOUT_S) -> float` — synchronous (for `asyncio.to_thread`), raises `OSError`/`ValueError`/`KeyError`/`TypeError` on any failure.
- Produces module globals: `GRID_SOURCE: str`, `P1_MONITOR_URL: str`.
- Produces (within `tick()`): a local `p1_result: tuple[bool, str] | None` — `None` when `GRID_SOURCE != "p1"`, else `(True, "OK")` on a successful P1 fetch or `(False, <reason>)` on a failed one. Task 4 consumes this.

- [ ] **Step 1: Write the failing tests**

First read `tests/test_dispatch_scheduler.py`'s top (imports, `T0`, `doc()`, `measurement_registers()`, `ScriptedClient`, `tick_with_cache`) to reuse its existing fixtures rather than inventing new ones. Add a new test class, e.g. after `TestDegradedFields` (~line 393-427):

```python
class TestP1GridSource:
    """GRID_SOURCE=p1 sources grid_w from the P1 monitor instead of REG_GRID_POWER, with
    the same surplus_w=None/batt_w=None fallback a bad Modbus read already has."""

    def test_p1_grid_w_overrides_the_register_reading(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "GRID_SOURCE", "p1")
        monkeypatch.setattr(scheduler, "fetch_p1_grid_w", lambda url, timeout=5: 300.0)
        # REG_GRID_POWER seeded to a very different value -- if this shows up in
        # surplus_w, the P1 override isn't wired.
        regs = measurement_registers(battery_power_w=-100)  # charging 100 W
        regs[R.REG_GRID_POWER] = 9999
        client = ScriptedClient(regs)
        cache: dict = {"released": False}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        # surplus_w = -(grid_w + batt_w); batt_w read raw is -100 (charging), P1 grid_w=300
        assert cache.get("p1_result") == (True, "OK")

    def test_p1_fetch_failure_falls_back_to_no_surplus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "GRID_SOURCE", "p1")

        def boom(url, timeout=5):
            raise OSError("no route to host")

        monkeypatch.setattr(scheduler, "fetch_p1_grid_w", boom)
        regs = measurement_registers()
        client = ScriptedClient(regs)
        cache: dict = {"released": False}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        assert cache.get("p1_result") == (False, "no route to host")

    def test_p1_malformed_response_falls_back_the_same_way(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "GRID_SOURCE", "p1")

        def bad(url, timeout=5):
            raise KeyError("active_power_w")

        monkeypatch.setattr(scheduler, "fetch_p1_grid_w", bad)
        regs = measurement_registers()
        client = ScriptedClient(regs)
        cache: dict = {"released": False}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        assert cache.get("p1_result")[0] is False

    def test_grid_source_inverter_default_never_calls_fetch_p1_grid_w(
            self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(scheduler, "fetch_p1_grid_w",
                            lambda url, timeout=5: called.append(1))
        regs = measurement_registers()
        client = ScriptedClient(regs)
        cache: dict = {"released": False}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        assert called == []
        assert cache.get("p1_result") is None
```

`ScriptedClient` and `measurement_registers` are already defined at the top of `tests/test_dispatch_scheduler.py` (this task's exploration confirmed both) — no new import needed, use them exactly as the rest of the file already does.

These tests read `cache["p1_result"]` — that means `tick()` must stash `p1_result` into `cache` so it's visible to a caller after the tick (the same way `cache["write_verified"]` and other tick-local facts are already exposed). Confirm this against Step 3 below.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dispatch_scheduler.py -v -k "P1GridSource"`
Expected: FAIL — `scheduler` has no `GRID_SOURCE` / `fetch_p1_grid_w` attributes yet.

- [ ] **Step 3: Add imports and module-level config**

In `dispatch/scheduler.py`, add to the imports near the top (alongside the existing `import json` etc., ~line 29):

```python
from urllib.request import urlopen
```

(Just this one line — `urllib.error.HTTPError`/`URLError` are caught implicitly via their common `OSError` base in Step 4 below, never referenced by name, so importing them would be an unused import.)

After `MONITOR_URLS` (ends ~line 68), add:

```python
# GRID_SOURCE gates whether tick() reads grid power from the inverter's own Modbus
# register (the default -- and the ONLY correct choice once the inverter is 3-phase
# again) or from a P1 monitor's local API. The inverter's grid CT only sees one phase
# on a house wired for three, so its own reading is wrong until the vendor replaces
# it -- see docs/superpowers/specs/2026-09-23-p1-grid-source-design.md.
GRID_SOURCE = os.environ.get("GRID_SOURCE", "inverter")
P1_MONITOR_URL = os.environ.get("P1_MONITOR_URL", "")
P1_FETCH_TIMEOUT_S = 5


def fetch_p1_grid_w(url: str, timeout: float = P1_FETCH_TIMEOUT_S) -> float:
    """Synchronous fetch of a P1 monitor's grid power -- run via asyncio.to_thread,
    never called directly from the event loop.

    Same sign convention as REG_GRID_POWER: positive = importing. Raises OSError
    (timeout, connection failure, non-2xx -- urllib.error.HTTPError subclasses
    URLError subclasses OSError) or ValueError/KeyError/TypeError on a malformed
    body. tick() catches all of these identically, exactly like a bad Modbus read.
    """
    with urlopen(url, timeout=timeout) as resp:
        body = json.load(resp)
    return float(body["active_power_w"])
```

- [ ] **Step 4: Wire it into `tick()`**

Replace the surplus block (find `# Surplus generation, for the one decision...` through the `except OSError as e:` block, ~line 639-663):

```python
    # Surplus generation, for the one decision that needs to know whether the sun is beating
    # the house: a charge whose target is already met freezes the battery, and freezing while
    # PV is spilling exports free solar (`slots._charge_target_reached`).
    #
    # Two registers rather than the grid meter alone, because grid power moves when we act and
    # generation-minus-load does not -- the identity and the measurements are in
    # `slots.SURPLUS_HARVEST_W`. A failed read is None, not zero: None falls back to the old
    # freeze, zero would claim the house is eating everything it makes.
    #
    # GRID_SOURCE=p1 replaces the register read with a fetch to a P1 monitor -- see
    # docs/superpowers/specs/2026-09-23-p1-grid-source-design.md. p1_result records the
    # fetch's own outcome (independent of the implausible-value check below) for the
    # p1-reachable Kuma monitor in monitor_pings() (Task 4).
    p1_result: tuple[bool, str] | None = None
    try:
        if GRID_SOURCE == "p1":
            grid_w = await asyncio.to_thread(fetch_p1_grid_w, P1_MONITOR_URL)
            p1_result = (True, "OK")
        else:
            grid_w = await inv.read(R.REG_GRID_POWER, 2, signed=True)
        batt_w = await inv.read(R.REG_BATTERY_POWER, signed=True)
        surplus_w = -(grid_w + batt_w)
        log.debug("surplus: grid=%+dW battery=%+dW -> %+dW", grid_w, batt_w, surplus_w)
        if abs(grid_w) > IMPLAUSIBLE_POWER_W or abs(batt_w) > IMPLAUSIBLE_POWER_W:
            # Neither register has ever been read by this process before 2026-08-20, so their
            # scale is documented rather than observed. A decode that is wrong by a factor is
            # the failure this guard is for, and the honest response is None -- the same
            # fallback as an unreadable register, i.e. the pre-existing freeze.
            log.warning("implausible power reading (grid=%+dW battery=%+dW) -- ignoring the "
                        "surplus rule this tick", grid_w, batt_w)
            surplus_w = None
            batt_w = None
    except (OSError, ValueError, KeyError, TypeError) as e:
        if GRID_SOURCE == "p1" and p1_result is None:
            p1_result = (False, str(e)[:200])
        log.warning("surplus read failed: %s -- a met charge target will hold, not release", e)
        surplus_w, batt_w = None, None
    cache["p1_result"] = p1_result
```

Note the `p1_result is None` guard in the `except` block: if the P1 fetch itself succeeded (`p1_result` already set to `(True, "OK")`) but the *subsequent* `inv.read(R.REG_BATTERY_POWER, ...)` call fails, don't overwrite `p1_result` — the P1 monitor was fine; the battery register wasn't. `cache["p1_result"] = p1_result` exposes it for Task 4's `monitor_pings()` call, and for the tests in Step 1 that read `cache.get("p1_result")`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_dispatch_scheduler.py -v -k "P1GridSource"`
Expected: PASS

Run: `pytest tests/test_dispatch_scheduler.py -v`
Expected: PASS (no regressions — in particular `TestDegradedFields` and anything else touching the surplus block)

- [ ] **Step 6: Write and verify the event-loop-offload test**

Add to the same `TestP1GridSource` class:

```python
    def test_p1_fetch_does_not_block_the_event_loop(self, tmp_path, monkeypatch):
        """A slow P1 fetch must not stall other coroutines -- asyncio.to_thread, not a
        direct blocking call. See scheduler.py's report()/heartbeat pattern this mirrors."""
        import time as time_mod

        monkeypatch.setattr(scheduler, "GRID_SOURCE", "p1")

        def slow_fetch(url, timeout=5):
            time_mod.sleep(0.2)
            return 300.0

        monkeypatch.setattr(scheduler, "fetch_p1_grid_w", slow_fetch)
        regs = measurement_registers()
        client = ScriptedClient(regs)
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        inv = scheduler.Inverter(client, 0x55, dry_run=True)
        cache: dict = {"released": False}

        ticks = 0

        async def counter():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.02)

        async def run_both():
            counter_task = asyncio.ensure_future(counter())
            await scheduler.tick(inv, slots_path, cache, T0)
            counter_task.cancel()

        asyncio.run(run_both())
        # 0.2s blocked / 0.02s tick-rate should give ~10 increments if the event loop
        # kept running during the fetch; a blocking call would give ~0-1.
        assert ticks >= 5
```

- [ ] **Step 7: Run this test in isolation a few times**

Run: `pytest tests/test_dispatch_scheduler.py -v -k "does_not_block" ` (repeat 3x)
Expected: PASS each time. If it's flaky (timing-sensitive), widen the margin (`ticks >= 3`) rather than deleting the test — the property it checks is real and matters.

- [ ] **Step 8: Full scheduler suite**

Run: `pytest tests/test_dispatch_scheduler.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add dispatch/scheduler.py tests/test_dispatch_scheduler.py
git commit -m "Wire GRID_SOURCE=p1 into the dispatch tick loop's surplus calc"
```

---

### Task 4: `dispatch/scheduler.py` — `p1-reachable` Kuma monitor

**Files:**
- Modify: `dispatch/scheduler.py` (`MONITOR_URLS`, ~line 60-68; `monitor_pings`, ~line 404-465; the call site, ~line 1090)
- Test: `tests/test_dispatch_monitors.py`

**Interfaces:**
- Consumes: `cache["p1_result"]` from Task 3.
- Modifies: `monitor_pings(decision, cache, live_soc, dry_run, p1_result=None)` — new optional 5th parameter.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_dispatch_monitors.py` in full first — it's short and this task changes its central fixtures (`MONITORS`, `pings()`).

Replace the `MONITORS` set (line 22-23):

```python
MONITORS = {"slots-fresh", "dispatcher-alive", "dispatch-confirmed",
            "inverter-not-hijacked", "soc-floor", "p1-reachable"}
```

Replace the `pings()` helper (line 25-27):

```python
def pings(decision, cache=None, live_soc=50.0, dry_run=False, p1_result=None):
    return dict((name, (status, msg)) for name, status, msg in
                scheduler.monitor_pings(decision, cache or {}, live_soc, dry_run, p1_result))
```

Replace `TestEveryDocumentedMonitorIsWired` (line 107-118):

```python
class TestEveryDocumentedMonitorIsWired:
    def test_the_url_table_covers_exactly_the_dispatcher_s_monitors(self):
        """The gap this file exists for: monitors in the design, none in the code."""
        assert set(scheduler.MONITOR_URLS) == MONITORS

    def test_a_healthy_tick_pings_the_five_always_wired_monitors(self):
        """p1-reachable is NOT one of these -- it's conditional on p1_result, since it's
        meaningless under GRID_SOURCE=inverter (no URL configured for it, either)."""
        sent = pings(commanded(), {"write_verified": True})
        assert set(sent) == MONITORS - {"p1-reachable"}
        assert all(status == "up" for status, _ in sent.values())

    def test_grid_source_p1_also_pings_p1_reachable_up(self):
        sent = pings(commanded(), {"write_verified": True}, p1_result=(True, "OK"))
        assert set(sent) == MONITORS
        assert sent["p1-reachable"] == ("up", "OK")

    def test_a_p1_failure_pings_p1_reachable_down_with_the_reason(self):
        sent = pings(commanded(), {"write_verified": True},
                     p1_result=(False, "no route to host"))
        assert sent["p1-reachable"] == ("down", "no route to host")

    def test_an_unset_url_makes_the_ping_a_no_op_rather_than_an_error(self, monkeypatch):
        """Monitors are created during go-live; the loop has to run before that."""
        monkeypatch.setitem(scheduler.MONITOR_URLS, "soc-floor", "")
        scheduler.send_heartbeat("", "up", "OK")  # must not raise, must not reach the network
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dispatch_monitors.py -v`
Expected: FAIL — `scheduler.MONITOR_URLS` doesn't have `p1-reachable` yet, and `monitor_pings()` doesn't accept a 5th arg.

- [ ] **Step 3: Add the monitor URL**

In `dispatch/scheduler.py`'s `MONITOR_URLS` dict (line 60-68), add:

```python
    "p1-reachable": os.environ.get("P1_REACHABLE_HEARTBEAT_URL", ""),
```

- [ ] **Step 4: Update `monitor_pings`**

Change the signature (line 404-405):

```python
def monitor_pings(decision: S.Decision, cache: dict, live_soc: float | None,
                  dry_run: bool, p1_result: tuple[bool, str] | None = None
                  ) -> list[tuple[str, str, str]]:
```

Update the docstring's "Three of the five" intro to say "the always-wired monitors" instead of hardcoding "five" (cosmetic, but keep it accurate):

Find:
```python
    """(monitor, status, message) for section 6.1's #4-#8. Pure -- the I/O is the caller's.

    All five are answered from one tick's worth of facts, so they are decided in one place;
```

Replace with:
```python
    """(monitor, status, message) for section 6.1's #4-#8, plus #10 (p1-reachable) when
    GRID_SOURCE=p1. Pure -- the I/O is the caller's.

    Every ping is answered from one tick's worth of facts, so they are decided in one place;
```

Just before the `return` statement (line 464-465), add:

```python
    if p1_result is not None:
        ok, msg = p1_result
        pings.append(("p1-reachable", "up" if ok else "down", msg))

    return [(name, status, msg[:200]) for name, status, msg in pings]
```

(Replacing the existing bare `return [(name, status, msg[:200]) for name, status, msg in pings]` line.)

- [ ] **Step 5: Update the call site**

In `tick()`, the call at line 1090:

```python
    await report(monitor_pings(decision, cache, live_soc, inv.dry_run), publisher)
```

becomes:

```python
    await report(
        monitor_pings(decision, cache, live_soc, inv.dry_run, cache.get("p1_result")),
        publisher)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_dispatch_monitors.py -v`
Expected: PASS

Run: `pytest tests/test_dispatch_scheduler.py -v`
Expected: PASS (the call-site change must not break anything there)

- [ ] **Step 7: Commit**

```bash
git add dispatch/scheduler.py tests/test_dispatch_monitors.py
git commit -m "Add p1-reachable Kuma monitor (#10)"
```

---

### Task 5: Config plumbing — `docker-compose.yml` and `.env.example`

**Files:**
- Modify: `docker-compose.yml` (`collector` service ~line 55-135, `dispatch` service ~line 323-395)
- Modify: `.env.example`

No tests — this is declarative config. Verify with `docker compose config` (Step 4).

- [ ] **Step 1: Add to the `collector` service's `environment:` block**

In `docker-compose.yml`, after `HEARTBEAT_URL: ${HEARTBEAT_URL:-}` in the `collector` service (~line 79), add:

```yaml
      # GRID_SOURCE=p1 replaces this collector's grid_power_w/load_power_w with a P1
      # monitor's local API instead of AlphaESS's own (currently phase-mismatched)
      # reading -- see docs/superpowers/specs/2026-09-23-p1-grid-source-design.md.
      # inverter (default) = today's behavior, unchanged.
      GRID_SOURCE: ${GRID_SOURCE:-inverter}
      P1_MONITOR_URL: ${P1_MONITOR_URL:-}
```

- [ ] **Step 2: Add to the `dispatch` service's `environment:` block**

In `docker-compose.yml`, after `SOC_FLOOR_HEARTBEAT_URL: ${SOC_FLOOR_HEARTBEAT_URL:-}` in the `dispatch` service (~line 384), add:

```yaml
      # Same GRID_SOURCE as the collector service -- keep both set the same way, or
      # live dispatch and historical recording disagree about which grid reading is
      # true. See docs/superpowers/specs/2026-09-23-p1-grid-source-design.md.
      GRID_SOURCE: ${GRID_SOURCE:-inverter}
      P1_MONITOR_URL: ${P1_MONITOR_URL:-}
      # Monitor #10, pinged by the control loop every tick, only when GRID_SOURCE=p1.
      # Catches: the loop is alive, but surplus-harvest decisions are blind because the
      # P1 monitor stopped answering.
      P1_REACHABLE_HEARTBEAT_URL: ${P1_REACHABLE_HEARTBEAT_URL:-}
```

- [ ] **Step 3: Add to `.env.example`**

In the collector section of `.env.example`, near `HEARTBEAT_URL=` (~line 182), add a commented block:

```
# GRID_SOURCE=p1 sources grid/load from a P1 monitor's local API instead of the
# AlphaESS inverter's own reading. Use this while the inverter is phase-mismatched
# with the house wiring (1-phase inverter, 3-phase house) -- its grid CT only sees
# one phase and both grid_power_w and load_power_w are wrong until the vendor
# replaces it. inverter (default) = today's behavior, unchanged.
GRID_SOURCE=inverter
# Required when GRID_SOURCE=p1. Full URL to the P1 monitor's local API, e.g.
# http://192.168.2.46/api/v1/data. Set the same way in the dispatch section below --
# a mismatch means live dispatch and historical recording disagree about grid power.
P1_MONITOR_URL=
```

In the dispatch section, near the `#4-#8` monitor block (after `SOC_FLOOR_HEARTBEAT_URL=`, ~line 335), add:

```
# GRID_SOURCE / P1_MONITOR_URL -- same meaning as the collector section above. Set
# both sections the same way.
GRID_SOURCE=inverter
P1_MONITOR_URL=

# Kuma "Push" monitor #10, pinged every tick, but ONLY when GRID_SOURCE=p1 --
# blank/unset is correct under GRID_SOURCE=inverter, not just "not set up yet".
# Catches: the loop is alive, but a P1 fetch failure means surplus-harvest
# decisions are silently falling back to freeze.
P1_REACHABLE_HEARTBEAT_URL=
```

- [ ] **Step 4: Verify the compose file parses**

Run: `docker compose config --quiet`
Expected: no output, exit code 0. (This will likely warn or fail on unset required vars like `INFLUX_TOKEN_COLLECTOR` if `.env` isn't fully populated locally — that's pre-existing and not this task's concern. If it fails specifically on something this task touched, fix it.)

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "Add GRID_SOURCE/P1_MONITOR_URL config to collector and dispatch services"
```

---

### Task 6: Docs — `DISPATCH-FLOW.md` and `DESIGN-dispatch.md`

**Files:**
- Modify: `docs/DISPATCH-FLOW.md`
- Modify: `DESIGN-dispatch.md` (repo root — **not** under `docs/`)

No tests — documentation only. Verify by rendering/reading the diffs.

- [ ] **Step 1: Update `docs/DISPATCH-FLOW.md`'s pipeline overview**

In the top `flowchart LR` (line 14), change:

```
    S["scheduler.py<br/>tick()<br/>read live SoC/grid/batt<br/>→ surplus_w"] -->|every 60s| D
```

to:

```
    S["scheduler.py<br/>tick()<br/>read live SoC/grid/batt<br/>(grid: inverter or P1, GRID_SOURCE)<br/>→ surplus_w"] -->|every 60s| D
```

- [ ] **Step 2: Update the live-decision flowchart's box C**

In the second `flowchart TD` (line 31), change:

```
    C["read live SoC / grid_w / battery_w<br/>→ surplus_w = −(grid_w + battery_w)"]
```

to:

```
    C["read live SoC / grid_w / battery_w<br/>grid_w: inverter register or P1 fetch (GRID_SOURCE)<br/>→ surplus_w = −(grid_w + battery_w)"]
```

- [ ] **Step 3: Add a note below the flowchart**

After the paragraph ending "...else hold." (~line 83), add a new paragraph:

```markdown
`GRID_SOURCE=p1` (default: `inverter`) replaces the `grid_w` register read with a fetch to a
P1 monitor's local API — for use while the AlphaESS inverter's own grid CT only sees one
phase of a 3-phase house. A P1 fetch failure folds into the same `surplus_w = None` /
`batt_w = None` fallback already shown above for an unreadable or implausible register — no
new branch in this diagram, only a new source for `grid_w`. See
`docs/superpowers/specs/2026-09-23-p1-grid-source-design.md` and `DESIGN-dispatch.md` §6.1
monitor #10.
```

- [ ] **Step 4: Add monitor #10 to `DESIGN-dispatch.md`'s §6.1 table**

Find the table (line 619-627):

```
| 9 | `dispatch-vs-plan` | daily job (§5.4) | 24 h + grace | All green, but the battery is not following the plan |
```

Add a new row right after it:

```
| 10 | `p1-reachable` | dispatcher | 5-15 min | GRID_SOURCE=p1 only: loop alive, but a P1 fetch failure has surplus-harvest blind and falling back to freeze |
```

- [ ] **Step 5: Add #10 to the narrative grouping**

Find the "Wake-up / Prompt but not nocturnal / Daily digest" groupings (~line 686-693). Add #10 to the "Prompt but not nocturnal" group (it shares #5/#6's shape: silent battery behavior, not a physical safety issue):

Find:
```
- **Prompt but not nocturnal:** #5, #6. No dispatch means self-consumption — a lost
```

Change to:
```
- **Prompt but not nocturnal:** #5, #6, #10. No dispatch means self-consumption — a lost
```

Read the rest of that sentence (it continues past what's shown here) and adjust it minimally so it still reads correctly with #10 folded in — don't just splice the number in if the following prose specifically describes #5/#6 in a way that would misdescribe #10; if so, add one clause covering #10's version of "not urgent, but should page a human by morning."

- [ ] **Step 6: Self-review the doc changes**

Re-read both changed docs end to end. Confirm: no other place in either file lists "five monitors" or "#4-#8" as an exhaustive claim now made stale by #10 (there's at least one in `monitor_pings()`'s docstring, already fixed in Task 4 — check the docs files specifically, not the code). Fix any you find.

- [ ] **Step 7: Commit**

```bash
git add docs/DISPATCH-FLOW.md DESIGN-dispatch.md
git commit -m "Document GRID_SOURCE and monitor #10 in DISPATCH-FLOW.md and DESIGN-dispatch.md"
```

---

### Task 7: Branch, push, and hand off for review

**Files:** none — git operations only.

- [ ] **Step 1: Confirm all tests pass together**

Run: `pytest tests/ -v`
Expected: PASS, full suite, no regressions anywhere (not just the files touched above).

Run: `ruff check .` (or however this repo's CI lints — check for a `pyproject.toml` ruff config or a CI workflow file if unsure)
Expected: clean.

- [ ] **Step 2: Review the full diff against the spec one more time**

Run: `git diff main --stat` and `git diff main` (against whatever the branch's base is). Walk through `docs/superpowers/specs/2026-09-23-p1-grid-source-design.md` section by section and confirm every part of it is reflected in the diff. Flag anything that drifted.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin <branch-name>
```

`gh pr create` does not work in this environment (the `gh` CLI here is authenticated read-only — confirmed in a prior session). Do not attempt it. Instead, after pushing, give the user the compare URL directly:

```
https://github.com/<owner>/<repo>/compare/main...<branch-name>
```

(Get `<owner>/<repo>` from `git remote get-url origin`.) The user opens that link themselves to create the PR through GitHub's UI, since `gh pr create` isn't available here.
