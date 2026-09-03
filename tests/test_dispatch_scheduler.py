"""The loop's error handling, and the bug that made all of it dead code.

`scheduler.py` degrades on `except OSError` in four places -- lose the SoC and decide without
it, lose the block read and skip the hijack check, lose the verify read and publish nothing
verified, lose the limits and run unclamped. Every one of those handlers was unreachable for
the failure it was written for, because `pymodbus.exceptions.ModbusIOException` -- what a
read TIMEOUT raises -- does not subclass OSError. It went straight past them into `run()`'s
catch-all, which kills the whole tick.

Observed in production on 2026-08-18, twice (13:03 and 16:00), each time two consecutive
ticks, each leaving a hole in `dispatch_state` that `review-dry-run.py` could only report as
"no decision" -- i.e. as a dispatcher that had stopped, which it had not.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest

import registers as R
import scheduler
import state as state_mod

pytest.importorskip("pymodbus", reason="pymodbus is not installed")

from pymodbus.exceptions import ModbusIOException

UTC = dt.UTC
T0 = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

TIMEOUT = ModbusIOException(
    "Modbus Error: [Input/Output] No response received after 3 retries, "
    "continue with next request")


def doc(**kw) -> dict:
    """The same minimal slots.json test_dispatch_slots.py uses: discharge 12:00-12:15."""
    return {
        "generated_at": "2026-08-01T11:55:00Z",
        "plan_run": "2026-08-01T09:00:00Z",
        "horizon_end": "2026-08-01T12:45:00Z",
        "interval_minutes": 15,
        "capacity_wh": 27900.0,
        "slots": [
            {"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z",
             "action": "discharge", "power_w": 4500, "target_soc": 20.0},
        ],
        **kw,
    }


def block_registers(active=0, mode=None, power_w=0, soc_pct=0.0, duration_s=0) -> dict[int, int]:
    """The nine DISPATCH_BLOCK words for a chosen decoded state, keyed by address."""
    mode = R.DispatchMode.FOLLOW if mode is None else mode
    regs: dict[int, int] = {R.REG_START: active, R.REG_MODE: mode}
    for i, w in enumerate(R.encode_power(power_w)):
        regs[R.REG_POWER + i] = w
    regs[R.REG_SOC] = R.encode_soc(soc_pct)[0]
    for i, w in enumerate(R.encode_int32(duration_s)):
        regs[R.REG_TIME + i] = w
    return regs


def measurement_registers(live_soc_pct=80.0, block=None, battery_power_w=0) -> dict[int, int]:
    """A plausible, quiet house: the dispatch block plus SoC/grid/battery, released and at
    rest, so a discharge decision's direction check (live SoC vs. target) has something to
    compare against.

    `battery_power_w` is discharge-positive raw register convention, same as
    `REG_BATTERY_POWER` itself -- a caller wanting a discharge reading passes a positive
    number here, the same sign the real register would hold.
    """
    regs = dict(block if block is not None else block_registers())
    regs[R.REG_BATTERY_SOC] = round(live_soc_pct * 10)
    regs[R.REG_GRID_POWER] = 0
    regs[R.REG_GRID_POWER + 1] = 0
    regs[R.REG_BATTERY_POWER] = int(battery_power_w) & 0xFFFF
    regs.update(voltage_registers())
    regs.update(temp_registers())
    regs.update(daily_registers())
    return regs


# The six blocks steps 8d and 8e read between them, and therefore how many ticks it takes for
# all of them to have had a turn under `SLOW_BLOCK_READS_PER_TICK`. Derived from the constant
# rather than hardcoded, so raising the budget does not silently make these helpers wrong.
SLOW_BLOCK_COUNT = 6


def settle_slow_blocks(tmp_path, monkeypatch, client, cache, start=None, step_s=60):
    """Run enough consecutive ticks for every due slow-tier block to get its turn.

    ONE SLOW READ PER TICK is the production behaviour (`SLOW_BLOCK_READS_PER_TICK`), so a
    single tick populates one block and a test asserting on a fresh process has to span the
    handful of ticks a real cold start does. The ticks are a minute apart, which is the real
    interval and is nothing against gates measured in days and weeks.
    """
    start = T0 if start is None else start
    ticks = -(-SLOW_BLOCK_COUNT // scheduler.SLOW_BLOCK_READS_PER_TICK)
    for i in range(ticks):
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=start + dt.timedelta(seconds=i * step_s))
    return ticks


def others_recently_read(*keep, now=None) -> dict:
    """Cache entries marking every slow block EXCEPT `keep` as just read.

    `SLOW_BLOCK_READS_PER_TICK` means a tick with several blocks due reads only one of them,
    in the order step 8d/8e happens to call them. A test about one block's own gate must not
    depend on that order, so it takes the others off the table instead.
    """
    now = T0 if now is None else now
    keys = ("weekly_firmware", "weekly_inverter_fw", "weekly_system_config",
            "daily_battery", "daily_inverter", "daily_pv")
    return {f"{k}_read_at": now for k in keys if k not in keep}


def fields_seen(pub, since=0) -> set[str]:
    """Every field name published across `pub`'s points from `since` onward.

    The slow tiers spread their reads over consecutive ticks, so "did this field get
    published" is a question about the run, not about one point -- and asking it of a single
    point is how a test ends up asserting on which tick a block happened to land on, which is
    a scheduling detail no dashboard cares about.
    """
    return {k for point in pub.points[since:] for k in point}


def daily_registers() -> dict[int, int]:
    """The daily tier's three blocks, keyed by address, holding the words the live inverter
    returned on 2026-09-03.

    Here for the same reason `voltage_registers` and `temp_registers` are: `ScriptedClient`
    zero-fills anything not given, and a zero block is precisely what
    `registers.daily_battery_plausible` and `lifetime_pv_plausible` exist to reject. Without
    this, every test in this file would exercise the daily tier's failure path while appearing
    to exercise nothing at all.
    """
    return {
        R.REG_SOH: 1000,                       # 100.0 %
        R.REG_LIFETIME_CHARGE: 0, R.REG_LIFETIME_CHARGE + 1: 10481,          # 1048.1 kWh
        R.REG_LIFETIME_DISCHARGE: 0, R.REG_LIFETIME_DISCHARGE + 1: 10221,    # 1022.1 kWh
        R.REG_LIFETIME_GRID_CHARGE: 0, R.REG_LIFETIME_GRID_CHARGE + 1: 5811,  # 581.1 kWh
        R.REG_INVERTER_TEMP: 370,              # 37.0 C
        R.REG_LIFETIME_PV: 1, R.REG_LIFETIME_PV + 1: 21175,                  # 867.11 kWh
    }


def voltage_registers(min_v=3.298, max_v=3.312, min_pack=3, max_pack=1,
                      min_cell=7, max_cell=12) -> dict[int, int]:
    """The six VOLTAGE_BLOCK words, keyed by address.

    Same reason as `temp_registers` for existing: `ScriptedClient` zero-fills anything not
    given, and a zero block is exactly the failure `registers.voltage_plausible` guards
    against. Same pack IDs as `temp_registers`' defaults, for no stronger reason than that
    they are the ones already confirmed live on this site (2026-08-27) -- voltage and
    temperature extremes are not guaranteed to share a pack in general.
    """
    return {
        R.REG_MIN_CELL_VOLTAGE_PACK: min_pack,
        R.REG_MIN_CELL_VOLTAGE_CELL: min_cell,
        R.REG_MIN_CELL_VOLTAGE: round(min_v * 1000),
        R.REG_MAX_CELL_VOLTAGE_PACK: max_pack,
        R.REG_MAX_CELL_VOLTAGE_CELL: max_cell,
        R.REG_MAX_CELL_VOLTAGE: round(max_v * 1000),
    }


def temp_registers(min_c=18.4, max_c=23.7, min_pack=3, max_pack=1,
                   min_cell=7, max_cell=12) -> dict[int, int]:
    """The six TEMP_BLOCK words, keyed by address.

    SEEDED INTO EVERY SCRIPTED TICK, and that is the point of it existing. `ScriptedClient`
    zero-fills any register it was not given, so without this every tick test here would
    quietly read a zero block -- and a zero block is exactly the failure
    `registers.temps_plausible` was hardened against, which would then be invisible to the
    one suite that drives the whole path. The pack IDs are the ones the live inverter
    reported on 2026-08-27: coldest cell in pack 3, hottest in pack 1.
    """
    return {
        R.REG_MIN_CELL_TEMP_PACK: min_pack,
        R.REG_MIN_CELL_TEMP_CELL: min_cell,
        R.REG_MIN_CELL_TEMP: round(min_c * 10) & 0xFFFF,
        R.REG_MAX_CELL_TEMP_PACK: max_pack,
        R.REG_MAX_CELL_TEMP_CELL: max_cell,
        R.REG_MAX_CELL_TEMP: round(max_c * 10) & 0xFFFF,
    }


def run_scripted_tick(tmp_path, monkeypatch, client, dry_run: bool, publisher,
                      slots_doc: dict | None = None):
    """`tick()` end-to-end against a `ScriptedClient`, mirroring `_tick` above but with a
    client that can answer reads truthfully instead of only ever timing out."""
    slots_path = tmp_path / "slots.json"
    slots_path.write_text(json.dumps(slots_doc if slots_doc is not None else doc()))
    monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
    inv = scheduler.Inverter(client, 0x55, dry_run=dry_run)
    cache: dict = {"released": False, "publisher": publisher}
    decision = asyncio.run(scheduler.tick(inv, slots_path, cache, T0))
    return decision, cache


class DeadClient:
    """A pymodbus client whose every call times out, exactly as the real one did."""

    def __init__(self):
        self.calls = 0

    async def read_holding_registers(self, addr, **kw):
        self.calls += 1
        raise TIMEOUT

    async def write_registers(self, addr, values, **kw):
        self.calls += 1
        raise TIMEOUT


class ScriptedClient:
    """A pymodbus client backed by a plain register dict, for driving `tick()` end-to-end
    against a CHOSEN inverter state -- as opposed to `DeadClient`, which can only exercise
    the every-read-fails path.

    `fail_addrs` raises the same timeout `DeadClient` does, but only for reads starting at
    one of those addresses -- e.g. the block read and not the SoC read. `latch_writes=False`
    is what stands in for a write that reaches the inverter but does not take: the write is
    still recorded, but the register dict never changes, so a verify readback keeps reporting
    the pre-write state.
    """

    def __init__(self, registers: dict[int, int], fail_addrs: frozenset[int] = frozenset(),
                latch_writes: bool = True):
        self.registers = dict(registers)
        self.fail_addrs = fail_addrs
        self.latch_writes = latch_writes
        self.writes: list[tuple[int, list[int]]] = []

    async def read_holding_registers(self, addr, count=1, **kw):
        if addr in self.fail_addrs:
            raise TIMEOUT
        values = [self.registers.get(addr + i, 0) for i in range(count)]
        return SimpleNamespace(isError=lambda: False, registers=values)

    async def write_registers(self, addr, values, **kw):
        self.writes.append((addr, list(values)))
        if self.latch_writes:
            for i, v in enumerate(values):
                self.registers[addr + i] = v
        return SimpleNamespace(isError=lambda: False)


def tick_with_cache(tmp_path, monkeypatch, client, cache: dict, now: dt.datetime = T0,
                    dry_run: bool = False, slots_doc: dict | None = None) -> scheduler.S.Decision:
    """Like `run_scripted_tick`, but the caller owns `cache` across the call -- needed to seed
    `cache["limits"]` or a `*_read_at` timestamp the way `run()` would, since
    `run_scripted_tick` always hands `tick()` a fresh cache."""
    slots_path = tmp_path / "slots.json"
    slots_path.write_text(json.dumps(slots_doc if slots_doc is not None else doc()))
    monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
    inv = scheduler.Inverter(client, 0x55, dry_run=dry_run)
    return asyncio.run(scheduler.tick(inv, slots_path, cache, now))


class RecordingPublisher:
    def __init__(self):
        self.points: list[dict] = []

    def publish(self, fields, now=None):
        self.points.append(fields)
        return True


class TestTheUpstreamFact:
    def test_modbus_io_exception_is_not_an_oserror(self):
        """The premise of every conversion below, pinned rather than assumed.

        If a future pymodbus makes ModbusIOException an OSError this test fails, and the
        right response is to DELETE the conversions -- not to keep them and wonder. Without
        this test that day looks like the fix was pointless all along.
        """
        assert not issubclass(ModbusIOException, OSError)
        assert issubclass(ModbusIOException, Exception)


class TestExceptionBoundary:
    """Everything crossing out of `Inverter` speaks OSError, whatever pymodbus raised."""

    def test_read_converts_a_timeout(self):
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        with pytest.raises(OSError, match="read 258 failed"):
            asyncio.run(inv.read(R.REG_BATTERY_SOC))

    def test_read_raw_block_converts_a_timeout(self):
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        with pytest.raises(OSError, match="dispatch block read failed"):
            asyncio.run(inv.read_raw_block())

    def test_read_temp_block_converts_a_timeout(self):
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        with pytest.raises(OSError, match="temp block read failed"):
            asyncio.run(inv.read_temp_block())

    def test_write_converts_a_timeout(self):
        # dry_run=False: the dry-run branch returns before touching the bus, so a dry-run
        # test here would pass no matter what the conversion did.
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=False)
        with pytest.raises(OSError, match="write 2176"):
            asyncio.run(inv.write(R.REG_START, [0]))

    def test_the_original_exception_is_kept_as_the_cause(self):
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        with pytest.raises(OSError) as caught:
            asyncio.run(inv.read(R.REG_BATTERY_SOC))
        assert isinstance(caught.value.__cause__, ModbusIOException)

    def test_limits_runs_unclamped_instead_of_propagating(self):
        """`limits()` is called BEFORE run()'s try block, so this one is a crash loop.

        An inverter that is unreachable at container start took the process down, and
        `restart: unless-stopped` brought it straight back to try again.
        """
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        assert asyncio.run(inv.limits()) == (None, None)

    def test_limits_are_read_independently(self):
        """A shared try around both reads originally meant a discharge-register timeout threw
        away a charge limit that had just been read successfully -- `slots.clamp()` already
        treats each half independently, so `limits()` must not be less granular than its own
        caller."""
        client = ScriptedClient({R.REG_MAX_CHARGE_POWER: 15000},
                                fail_addrs=frozenset({R.REG_MAX_DISCHARGE_POWER}))
        inv = scheduler.Inverter(client, 0x55, dry_run=True)
        assert asyncio.run(inv.limits()) == (15000, None)


class TestTickSurvivesAnUnreadableInverter:
    def _tick(self, tmp_path, publisher):
        import json
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        inv = scheduler.Inverter(DeadClient(), 0x55, dry_run=True)
        cache: dict = {"released": False, "publisher": publisher}
        return asyncio.run(scheduler.tick(inv, slots_path, cache, T0)), cache

    def test_it_still_decides(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        decision, _ = self._tick(tmp_path, RecordingPublisher())
        # The slot is live at T0, so the decision stands on the plan alone -- losing the SoC
        # costs the direction check, not the decision.
        assert decision.kind in ("command", "release", "idle")
        assert decision.slot is not None

    def test_it_still_publishes_the_decision(self, tmp_path, monkeypatch):
        """The half that turns a hole into an event.

        Before this, an unreadable inverter published NOTHING, and `review-dry-run.py` pivots
        on exactly these fields -- so the tick vanished from the series and the page reported
        a dispatcher that had stopped.
        """
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        pub = RecordingPublisher()
        self._tick(tmp_path, pub)
        assert len(pub.points) == 1
        point = pub.points[0]
        assert point["slot_action"] == "discharge"
        assert point["plan_run"] == "2026-08-01T09:00:00Z"
        assert "No response received" in point["read_error"]

    def test_it_publishes_no_register_it_could_not_read(self, tmp_path, monkeypatch):
        # The point of the degraded point is that it does not lie. A stale `action` carried
        # forward would render on the dashboard as a live command nobody verified.
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        pub = RecordingPublisher()
        self._tick(tmp_path, pub)
        point = pub.points[0]
        for absent in ("action", "setpoint_w", "dispatch_active", "mode", "raw_0880"):
            assert absent not in point

    def test_a_failed_surplus_read_holds_rather_than_guessing(self, tmp_path, monkeypatch):
        """None is not zero. A dead client can't read the grid/battery registers either, and
        the fallback must be the pre-existing freeze, not a guessed surplus that could
        spuriously release the battery."""
        heartbeat = tmp_path / "hb.json"
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", heartbeat)
        self._tick(tmp_path, RecordingPublisher())
        payload = json.loads(heartbeat.read_text())
        assert payload["surplus_w"] is None


class TestDegradedFields:
    def test_it_carries_the_decision_and_the_reason(self):
        fields = state_mod.build_degraded_fields(
            slot={"start": "2026-08-01T12:00:00Z", "action": "hold"},
            plan_run="2026-08-01T09:00:00Z",
            read_error="timed out")
        assert fields == {
            "read_error": "timed out",
            "slot_start": int(dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC).timestamp()),
            "slot_action": "hold",
            "plan_run": "2026-08-01T09:00:00Z",
            # What the dispatcher knew, which no failed read can take away. Defaults here
            # because this call passes none of them; `tests/test_dispatch_state.py` covers
            # them being carried through.
            "decision_kind": "unknown",
            "reason": "unspecified",
            "live": 0,
        }

    def test_outside_a_slot_it_is_just_the_reason(self):
        assert state_mod.build_degraded_fields() == {
            "read_error": "inverter unreadable",
            "decision_kind": "unknown",
            "reason": "unspecified",
            "live": 0,
        }

    def test_its_fields_are_ones_the_review_page_pivots_on(self):
        """Not decoration: `review-dry-run.py` only sees a tick if one of these is present."""
        pivoted = {"slot_action", "action", "plan_run", "setpoint_w", "dispatch_active"}
        fields = state_mod.build_degraded_fields(
            slot={"start": "2026-08-01T12:00:00Z", "action": "hold"}, plan_run="x")
        assert set(fields) & pivoted


class TestTickPublishesTheWriteVerifyVerdict:
    """`tick()` end-to-end, not `state.build_fields()` called by hand -- these pin the same
    contract `TestDegradedFields` pins for the degraded shape, but for the live shape, and for
    monitor #6's own scenario: a write that reaches the inverter and does not take."""

    def test_a_write_that_never_lands_publishes_verified_zero(self, tmp_path, monkeypatch):
        # `latch_writes=False`: the write is recorded but the register dict never moves, so
        # the verify readback keeps reporting the pre-write, released state -- exactly what
        # "the block does not hold what was just written" looks like. The point is published
        # on the FIRST such tick, before the alarm waits for a second one: `verified=0` is
        # the honest reading of what the block held, and the debounce is about the alarm.
        client = ScriptedClient(measurement_registers(), latch_writes=False)
        pub = RecordingPublisher()
        decision, cache = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        assert decision.kind == "command"
        assert cache["write_verified"] is False
        point = pub.points[0]
        assert point["verified"] == 0

    def test_a_write_that_lands_publishes_verified_one(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers(), latch_writes=True)
        pub = RecordingPublisher()
        decision, cache = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        assert decision.kind == "command"
        assert cache["write_verified"] is True
        point = pub.points[0]
        assert point["verified"] == 1
        # The gap this closes: `live` was only ever asserted against `build_fields()` called
        # directly, never derived by `tick()` itself from a real `inv.dry_run=False`.
        assert point["live"] == 1

    def test_a_release_tick_publishes_no_verified_field(self, tmp_path, monkeypatch):
        # Nothing was commanded this tick, so there is nothing to confirm -- `verified` must
        # be ABSENT, not `0`. A `0` here would read as "we tried and failed", which release
        # never does.
        client = ScriptedClient(measurement_registers(), latch_writes=True)
        pub = RecordingPublisher()
        release_doc = doc(slots=[
            {"start": "2026-08-01T12:00:00Z", "end": "2026-08-01T12:15:00Z", "action": "self"},
        ])
        decision, cache = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=True,
                                            publisher=pub, slots_doc=release_doc)
        assert decision.kind == "release"
        assert cache["write_verified"] is None
        point = pub.points[0]
        assert "verified" not in point
        # Dry run, so this must read 0 even though a live command elsewhere in this class
        # reads 1 -- `live` is `not inv.dry_run`, not "did we write something".
        assert point["live"] == 0


class TestTickPublishesADegradedPointWhenOnlyTheBlockReadFails:
    """The SoC read and the dispatch-block read are two separate registers, and the common
    failure (per `state.build_degraded_fields`'s own docstring) is the second one alone.
    Previously only exercised by calling `build_degraded_fields()` directly with a hand-built
    `live_soc_pct`; this drives the same split through a real `tick()`."""

    def test_soc_read_survives_a_failed_block_read(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers(live_soc_pct=80.0),
                                fail_addrs=frozenset({R.REG_START}))
        pub = RecordingPublisher()
        decision, cache = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        # The plan still decides on the SoC it did manage to read.
        assert decision.kind == "command"
        assert cache["write_verified"] is None
        point = pub.points[0]
        assert point["soc_pct"] == pytest.approx(80.0)
        assert "read_error" in point
        # The block could not be read, so nothing derived from it is published -- carrying
        # any of these forward would show a command as live before it was ever confirmed.
        for absent in ("dispatch_active", "action", "setpoint_w", "mode", "verified"):
            assert absent not in point


class TestActualBatteryReading:
    """`REG_BATTERY_POWER`, read every tick for the surplus rule (step 4) and now published
    alongside `setpoint_w` -- the finding that prompted it: MEASURED 2026-08-24, a commanded
    4,700 W discharge settling at ~4,400 W for a full session, invisible anywhere but an ad
    hoc Influx query until this field existed."""

    def test_it_is_published_charging_positive(self, tmp_path, monkeypatch):
        # 3,200 W discharging, in the REGISTER's discharge-positive convention.
        client = ScriptedClient(measurement_registers(battery_power_w=3200))
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        assert pub.points[0]["actual_battery_w"] == -3200.0

    def test_it_survives_a_failed_block_read(self, tmp_path, monkeypatch):
        """The dispatch block and the battery-power register are two separate reads, same
        argument `TestTickPublishesADegradedPointWhenOnlyTheBlockReadFails` makes for SoC."""
        client = ScriptedClient(measurement_registers(battery_power_w=3200),
                                fail_addrs=frozenset({R.REG_START}))
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["actual_battery_w"] == -3200.0
        assert "read_error" in point


class TestCellVoltage:
    """The wiring, mirroring `TestCellTemperature` immediately below -- same shape, a
    separate read and a separate error variable (see step 8b's comment for why)."""

    def test_the_voltages_reach_the_published_point(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["min_cell_voltage_v"] == 3.298
        assert point["max_cell_voltage_v"] == 3.312
        assert point["min_cell_voltage_pack"] == 3
        assert point["max_cell_voltage_pack"] == 1

    def test_they_survive_a_failed_block_read(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.REG_START}))
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["min_cell_voltage_v"] == 3.298
        assert "read_error" in point

    def test_a_failed_voltage_read_does_not_fail_the_tick(self, tmp_path, monkeypatch, caplog):
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.VOLTAGE_BLOCK[0]}))
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            decision, _ = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        assert decision.kind in ("command", "release", "idle")
        point = pub.points[0]
        assert not [k for k in point if "cell_voltage" in k]
        assert "setpoint_w" in point
        assert "voltage block read failed" in caplog.text

    def test_an_all_zero_block_publishes_nothing_rather_than_a_dead_cell(
            self, tmp_path, monkeypatch, caplog):
        regs = measurement_registers()
        for addr in range(R.VOLTAGE_BLOCK[0], R.VOLTAGE_BLOCK[0] + R.VOLTAGE_BLOCK[1]):
            regs[addr] = 0
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            run_scripted_tick(tmp_path, monkeypatch, ScriptedClient(regs), dry_run=False,
                              publisher=pub)
        assert not [k for k in pub.points[0] if "cell_voltage" in k]
        assert "implausible cell voltages" in caplog.text

    def test_an_implausible_scale_publishes_nothing(self, tmp_path, monkeypatch, caplog):
        """Raw 32980/33120 is what this block would read if the scale were 0.0001 V/bit
        rather than 0.001 -- the failure VOLTAGE_PLAUSIBLE_V exists for."""
        regs = measurement_registers()
        regs.update(voltage_registers(min_v=32.98, max_v=33.12))
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            run_scripted_tick(tmp_path, monkeypatch, ScriptedClient(regs), dry_run=False,
                              publisher=pub)
        assert not [k for k in pub.points[0] if "cell_voltage" in k]
        assert "implausible cell voltages" in caplog.text

    def test_a_short_read_degrades_instead_of_killing_the_tick(self, tmp_path, monkeypatch,
                                                               caplog):
        class ShortVoltageClient(ScriptedClient):
            async def read_holding_registers(self, addr, count=1, **kw):
                r = await super().read_holding_registers(addr, count=count, **kw)
                if addr == R.VOLTAGE_BLOCK[0]:
                    return SimpleNamespace(isError=lambda: False, registers=r.registers[:4])
                return r

        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            decision, _ = run_scripted_tick(
                tmp_path, monkeypatch, ShortVoltageClient(measurement_registers()),
                dry_run=False, publisher=pub)
        assert decision is not None
        point = pub.points[0]
        assert not [k for k in point if "cell_voltage" in k]
        assert "setpoint_w" in point
        assert "expected 6 words, got 4" in caplog.text

    def test_a_persistent_failure_warns_once_and_then_goes_quiet(self, tmp_path, monkeypatch,
                                                                 caplog):
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.VOLTAGE_BLOCK[0]}))
        inv = scheduler.Inverter(client, 0x55, dry_run=False)
        cache: dict = {"released": False, "publisher": RecordingPublisher()}

        with caplog.at_level("WARNING"):
            for i in range(3):
                asyncio.run(scheduler.tick(inv, slots_path, cache,
                                           T0 + dt.timedelta(seconds=60 * i)))
        assert len([r for r in caplog.records
                   if "voltage block read failed" in r.message]) == 1

    def test_a_recovery_is_announced_so_the_quiet_is_readable(self, tmp_path, monkeypatch,
                                                             caplog):
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.VOLTAGE_BLOCK[0]}))
        inv = scheduler.Inverter(client, 0x55, dry_run=False)
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        asyncio.run(scheduler.tick(inv, slots_path, cache, T0))

        client.fail_addrs = frozenset()
        with caplog.at_level("INFO"):
            asyncio.run(scheduler.tick(inv, slots_path, cache, T0 + dt.timedelta(seconds=60)))
        assert "cell voltage readings recovered" in caplog.text


class TestCellTemperature:
    """The wiring, which is where this feature actually lives.

    `decode_temp_block`, `temps_plausible` and `build_fields(temps=...)` are each tested on
    their own elsewhere. What those cannot show is that `tick()` joins them up -- reads the
    block, refuses a bad one, survives a failure, and puts the result on the point and in the
    log line an operator reads.
    """

    def test_the_temperatures_reach_the_published_point(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["min_cell_temp_c"] == 18.4
        assert point["max_cell_temp_c"] == 23.7
        # The pack IDs travel with them: a hot cell is only actionable once you know which of
        # the three boxes to open.
        assert point["min_cell_temp_pack"] == 3
        assert point["max_cell_temp_pack"] == 1

    def test_they_survive_a_failed_block_read(self, tmp_path, monkeypatch):
        """A degraded point still carries them -- two separate reads, and the dispatch block
        is the commoner failure. Same argument as `actual_battery_w` above."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.REG_START}))
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["min_cell_temp_c"] == 18.4
        assert "read_error" in point

    def test_a_failed_temp_read_does_not_fail_the_tick(self, tmp_path, monkeypatch, caplog):
        """The whole safety claim of this feature in one test: temperature is observability,
        and observability must never be able to cost a decision."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.TEMP_BLOCK[0]}))
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            decision, _ = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        assert decision.kind in ("command", "release", "idle")
        point = pub.points[0]
        assert not [k for k in point if "cell_temp" in k]
        # The command still went out -- the failure cost the field and nothing else.
        assert "setpoint_w" in point
        assert "temp block read failed" in caplog.text

    def test_an_all_zero_block_publishes_nothing_rather_than_a_freezing_battery(
            self, tmp_path, monkeypatch, caplog):
        """A BMS that has stopped answering returns zeros, which decode to a perfectly
        plausible 0.0 C. Publishing that is the exact lie this module refuses everywhere
        else, and the bounds alone do not catch it -- the 1-based pack ID does."""
        regs = measurement_registers()
        for addr in range(R.TEMP_BLOCK[0], R.TEMP_BLOCK[0] + R.TEMP_BLOCK[1]):
            regs[addr] = 0
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            run_scripted_tick(tmp_path, monkeypatch, ScriptedClient(regs), dry_run=False,
                              publisher=pub)
        assert not [k for k in pub.points[0] if "cell_temp" in k]
        assert "implausible cell temperatures" in caplog.text

    def test_an_implausible_scale_publishes_nothing(self, tmp_path, monkeypatch, caplog):
        """Raw 1840/2370 is what this block would read if the scale were 1 C/bit rather than
        0.1 -- the failure `TEMP_PLAUSIBLE_C` exists for, driven through the loop."""
        regs = measurement_registers()
        regs.update(temp_registers(min_c=184.0, max_c=237.0))
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            run_scripted_tick(tmp_path, monkeypatch, ScriptedClient(regs), dry_run=False,
                              publisher=pub)
        assert not [k for k in pub.points[0] if "cell_temp" in k]
        assert "implausible cell temperatures" in caplog.text

    def test_a_short_read_degrades_instead_of_killing_the_tick(self, tmp_path, monkeypatch,
                                                               caplog):
        """`decode_temp_block` raises ValueError, not OSError, when a device answers with
        fewer words than were asked for. Caught beside OSError in `tick()`, because an
        uncaught one reaches `run()`'s catch-all and costs the whole `dispatch_state` point
        for that minute -- the 2026-08-18 shape this file's docstring is about."""

        class ShortTempClient(ScriptedClient):
            async def read_holding_registers(self, addr, count=1, **kw):
                r = await super().read_holding_registers(addr, count=count, **kw)
                if addr == R.TEMP_BLOCK[0]:
                    return SimpleNamespace(isError=lambda: False, registers=r.registers[:4])
                return r

        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            decision, _ = run_scripted_tick(
                tmp_path, monkeypatch, ShortTempClient(measurement_registers()),
                dry_run=False, publisher=pub)
        assert decision is not None
        point = pub.points[0]
        assert not [k for k in point if "cell_temp" in k]
        assert "setpoint_w" in point          # the tick completed and published a full point
        assert "expected 6 words, got 4" in caplog.text

    def test_a_persistent_failure_warns_once_and_then_goes_quiet(self, tmp_path, monkeypatch,
                                                                 caplog):
        """A block this firmware does not support fails every 60 s forever, which at one
        WARNING a tick is 1,440 identical lines a day burying everything that matters. The
        repeats drop to debug; the first still shouts."""
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.TEMP_BLOCK[0]}))
        inv = scheduler.Inverter(client, 0x55, dry_run=False)
        cache: dict = {"released": False, "publisher": RecordingPublisher()}

        with caplog.at_level("WARNING"):
            for i in range(3):
                asyncio.run(scheduler.tick(inv, slots_path, cache,
                                           T0 + dt.timedelta(seconds=60 * i)))
        assert len([r for r in caplog.records if "temp block read failed" in r.message]) == 1

    def test_a_recovery_is_announced_so_the_quiet_is_readable(self, tmp_path, monkeypatch,
                                                             caplog):
        """Without this line, "no warnings lately" means either fixed or still broken and
        muted -- the failure mode of every rate-limited log."""
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.TEMP_BLOCK[0]}))
        inv = scheduler.Inverter(client, 0x55, dry_run=False)
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        asyncio.run(scheduler.tick(inv, slots_path, cache, T0))

        client.fail_addrs = frozenset()
        with caplog.at_level("INFO"):
            asyncio.run(scheduler.tick(inv, slots_path, cache, T0 + dt.timedelta(seconds=60)))
        assert "cell temperature readings recovered" in caplog.text

    def test_the_log_line_carries_both_extremes(self, tmp_path, monkeypatch, caplog):
        """DEPLOY.md documents this line field by field, which makes its format an operator
        interface rather than a debug aid."""
        with caplog.at_level("INFO"):
            run_scripted_tick(tmp_path, monkeypatch, ScriptedClient(measurement_registers()),
                              dry_run=False, publisher=RecordingPublisher())
        assert "temp=18.4/23.7C" in caplog.text

    def test_the_log_line_says_question_mark_when_unread(self, tmp_path, monkeypatch, caplog):
        """The formatting is inline in the `log.info` call, so a None reaching the format
        expression would raise inside logging itself -- a tick killed by its own audit line."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.TEMP_BLOCK[0]}))
        with caplog.at_level("INFO"):
            run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                              publisher=RecordingPublisher())
        assert "temp=?" in caplog.text


class TestFaultBlock:
    """The hourly health tier's wiring -- step 8c. `decode_fault_block` is tested on its own
    in `test_dispatch_registers.py`; this shows `tick()` joins it up the same way step 8b
    already does for temperature."""

    def test_the_fault_fields_reach_the_published_point_on_a_fresh_process(
            self, tmp_path, monkeypatch):
        """A fresh `cache` has no `health_read_at` -- that counts as due, so the very first
        tick after a cold start publishes these fields rather than waiting an hour."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False, publisher=pub)
        point = pub.points[0]
        assert point["fault_raw_0131"] == 0
        assert point["active_fault_count"] == 0
        assert point["active_warning_count"] == 0

    def test_a_failed_fault_read_does_not_fail_the_tick(self, tmp_path, monkeypatch, caplog):
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.FAULT_BLOCK[0]}))
        pub = RecordingPublisher()
        with caplog.at_level("WARNING"):
            decision, _ = run_scripted_tick(tmp_path, monkeypatch, client, dry_run=False,
                                            publisher=pub)
        assert decision.kind in ("command", "release", "idle")
        point = pub.points[0]
        assert not [k for k in point if k.startswith("fault_")]
        # The counts go with the words. A failed read publishes NO count rather than zero:
        # "nothing is wrong" and "we could not ask" must not render as the same green tile.
        assert "active_fault_count" not in point
        assert "active_warning_count" not in point
        assert "setpoint_w" in point
        assert "fault block read failed" in caplog.text

    def test_skipped_entirely_when_the_tick_already_has_a_read_error(
            self, tmp_path, monkeypatch):
        """A failed dispatch-block read (`REG_START`) sets `read_error` before step 8c runs --
        the inverter is very likely unreachable, so piling a fault-block read on top of that is
        a second ~12 s timeout paid for a read almost certain to fail too. `limits_hourly` is
        skipped along with it, even though republishing it costs no I/O -- one shared gate
        condition, matching 8c's own comment."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.REG_START}))
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "limits": (15000, 13000)}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        point = cache["publisher"].points[0]
        assert "fault_raw_0131" not in point
        assert "max_charge_power_w" not in point
        # The gate itself must stay due -- skipped, not attempted-and-failed -- so the very
        # next tick tries again once the inverter answers.
        assert "health_read_at" not in cache

    def test_the_inverter_limits_are_republished_under_health_field_names(
            self, tmp_path, monkeypatch):
        """Not a new register read: `cache["limits"]` is already refreshed hourly by `run()`'s
        LIMITS_REFRESH_S for the clamp, so this just exposes that value under new names."""
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "limits": (15000, 13000)}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        point = cache["publisher"].points[0]
        assert point["max_charge_power_w"] == 15000
        assert point["max_discharge_power_w"] == 13000

    def test_an_unread_limit_half_is_absent_not_zero(self, tmp_path, monkeypatch):
        """`Inverter.limits()` degrades each half independently to None on its own failure --
        see `TestExceptionBoundary`. A `None` half must stay absent, not become a 0 W ceiling
        nobody commanded."""
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "limits": (15000, None)}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        point = cache["publisher"].points[0]
        assert point["max_charge_power_w"] == 15000
        assert "max_discharge_power_w" not in point


class TestWeeklyHealthBlocks:
    """The weekly health tier's wiring -- step 8d. A tripwire, not a trend, so lighter than
    `TestFaultBlock`, but with one thing `TestFaultBlock` does not need to prove: the three
    blocks are read independently, so one failing must not silence the other two."""

    def test_the_weekly_fields_reach_the_published_point_on_a_fresh_process(
            self, tmp_path, monkeypatch):
        """ACROSS THE FIRST FEW TICKS, not on the first one. `SLOW_BLOCK_READS_PER_TICK` caps
        the slow tiers at one block per tick precisely so a cold start, where all six are due
        at once, cannot spend six timeout ladders inside a 60 s control loop."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        assert "firmware_raw_0115" in seen
        assert "inverter_fw_raw_0640" in seen
        assert "system_config_raw_0800" in seen

    def test_a_cold_start_reads_at_most_one_slow_block_per_tick(self, tmp_path, monkeypatch):
        """The bound itself. Every slow gate is empty on a fresh process, and each of these
        reads can cost the client's full ~12 s retry ladder against a range the inverter does
        not support -- six of those plus the fault block overruns the tick, and `next_deadline`
        does not catch up, so the loop quietly runs at 120 s and `reliability.py` reports the
        missing ticks as findings about something else."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        gates = [k for k in cache if k.endswith("_read_at") and k != "health_read_at"]
        assert len(gates) == scheduler.SLOW_BLOCK_READS_PER_TICK, gates

    def test_a_block_skipped_for_budget_stays_due(self, tmp_path, monkeypatch):
        """Deferred, never dropped: the skip touches no gate, so the block is read on the next
        tick rather than waiting out its own week."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert "weekly_inverter_fw_read_at" not in cache
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=1))
        assert "weekly_inverter_fw_read_at" in cache

    def test_a_failed_firmware_read_does_not_silence_the_other_two_blocks(
            self, tmp_path, monkeypatch, caplog):
        """ONE TRY PER BLOCK, not one try around all three -- a firmware block this hardware
        does not support must not cost the inverter-firmware and system-config blocks, which
        read back fine."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.FIRMWARE_BLOCK[0]}))
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        with caplog.at_level("WARNING"):
            settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        assert not [k for k in seen if k.startswith("firmware_raw_")]
        assert "inverter_fw_raw_0640" in seen
        assert "system_config_raw_0800" in seen
        assert "setpoint_w" in seen
        assert "firmware block read failed" in caplog.text

    def test_a_failure_of_all_three_blocks_does_not_fail_the_tick(
            self, tmp_path, monkeypatch, caplog):
        client = ScriptedClient(measurement_registers(), fail_addrs=frozenset(
            {R.FIRMWARE_BLOCK[0], R.INVERTER_FW_BLOCK[0], R.SYSTEM_CONFIG_BLOCK[0]}))
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        with caplog.at_level("WARNING"):
            settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        for prefix in ("firmware_raw_", "inverter_fw_raw_", "system_config_raw_"):
            assert not [k for k in seen if k.startswith(prefix)]
        assert "setpoint_w" in seen

    def test_a_failure_backs_off_to_the_hourly_interval_not_the_full_week(
            self, tmp_path, monkeypatch):
        """Previously the whole tier's shared timestamp was stamped unconditionally, so one
        transient timeout cost a full week before the next attempt. The failing block must now
        retry within the hour."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.FIRMWARE_BLOCK[0]}))
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert cache["weekly_firmware_fail_streak"] == 1

        client.fail_addrs = frozenset()
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=90))
        assert "firmware_raw_0115" in pub.points[1]

    def test_a_failing_block_does_not_slow_its_siblings_retry(self, tmp_path, monkeypatch):
        """THE BUG THIS CLASS OF FIX EXISTS FOR: an earlier version shared one timestamp
        across all three blocks, so a lone persistently-failing block (system config, say)
        dragged the other two -- which read back fine every time -- onto its own hourly retry
        cadence instead of letting them settle back onto their normal week. Each block's own
        gate must be untouched by its siblings' fortunes."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.SYSTEM_CONFIG_BLOCK[0]}))
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        ticks = settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        assert "firmware_raw_0115" in seen                 # firmware read back fine
        assert "system_config_raw_0800" not in seen

        # A day later: system config is still down, but firmware and inverter firmware must
        # NOT be due again yet -- they succeeded, so their own gate is the full week, and
        # nothing about system config's own trouble should have touched it.
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(days=1))
        assert "firmware_raw_0115" not in pub.points[ticks]
        assert "inverter_fw_raw_0640" not in pub.points[ticks]

    def test_a_persistent_failure_gives_up_after_the_streak_limit(
            self, tmp_path, monkeypatch):
        """A register that fails WEEKLY_FAIL_STREAK_LIMIT hourly attempts in a row is not
        transient -- it is a register this hardware does not have. Past the limit, retrying it
        hourly forever is exactly the Modbus-budget risk the `read_error` skip elsewhere in
        this file exists to bound, so the interval must revert to the full week."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.FIRMWARE_BLOCK[0]}))
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        now = T0
        for _ in range(scheduler.WEEKLY_FAIL_STREAK_LIMIT):
            tick_with_cache(tmp_path, monkeypatch, client, cache, now=now)
            now += dt.timedelta(hours=1, minutes=1)
        assert cache["weekly_firmware_fail_streak"] == scheduler.WEEKLY_FAIL_STREAK_LIMIT

        # One more hour later: still within the old hourly cadence, but the streak limit has
        # been reached, so this must NOT attempt again -- the gate has reverted to the week.
        pub = cache["publisher"]
        n_points_before = len(pub.points)
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=now + dt.timedelta(hours=1))
        assert cache["weekly_firmware_fail_streak"] == scheduler.WEEKLY_FAIL_STREAK_LIMIT
        assert len(pub.points) == n_points_before + 1  # the tick still ran, just skipped 8d

    def test_a_success_restores_the_full_week_interval(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert cache["weekly_firmware_fail_streak"] == 0

        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(hours=2))
        assert "firmware_raw_0115" not in pub.points[1]

    def test_a_second_block_newly_failing_gets_its_own_warning(
            self, tmp_path, monkeypatch, caplog):
        """Independent error-transition tracking per block, not one shared string: a second,
        DIFFERENT block failing for the first time must not hide behind a first block's
        already-quieted (debug-level) repeat failures.

        System config succeeded on the first tick, so its own gate does not come due again
        for a full week -- unlike firmware, which is already failing and therefore due again
        within the hour. Waited out here (via a synthetic 8-day gap, not a real wait) so both
        blocks are genuinely due on the second tick, rather than asserting on a block whose
        gate a shorter gap would have skipped entirely.
        """
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.FIRMWARE_BLOCK[0]}))
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        with caplog.at_level("WARNING"):
            # Every slow block gets its first turn, one per tick -- system config included, so
            # its own weekly gate is genuinely set before the 8-day gap below reopens it.
            settle_slow_blocks(tmp_path, monkeypatch, client, cache)
            caplog.clear()
            # Firmware fails again (now debug-only, already warned), and system config fails
            # for the first time once its own weekly gate comes due -- that failure must still
            # warn on its own.
            client.fail_addrs = frozenset({R.FIRMWARE_BLOCK[0], R.SYSTEM_CONFIG_BLOCK[0]})
            # Several ticks again, not one: firmware is due every hour while it is failing, so
            # it takes the first tick's single slow read and system config only gets its turn
            # on a later one.
            settle_slow_blocks(tmp_path, monkeypatch, client, cache,
                               start=T0 + dt.timedelta(days=8))
        assert "system config block read failed" in caplog.text
        assert "firmware block read failed" not in caplog.text  # already warned once, now quiet

    def test_skipped_entirely_when_the_tick_already_has_a_read_error(
            self, tmp_path, monkeypatch):
        """Same reasoning as the fault block's own version of this test: three MORE block
        reads on top of an already-unreachable inverter is the worst case that motivated the
        skip -- up to four extra ~12 s timeouts on a fresh process where both gates are due."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.REG_START}))
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        point = cache["publisher"].points[0]
        assert "firmware_raw_0115" not in point
        assert "weekly_firmware_read_at" not in cache


class TestHealthGates:
    """The literal acceptance check: hourly/weekly fields appear only once their gate has
    elapsed, and do not re-fire on the very next tick. `cache["health_read_at"]` and
    `cache["weekly_firmware_read_at"]` (one of three independent weekly-block keys -- see
    `TestWeeklyHealthBlocks` for why they're separate) are what `run()` would maintain across
    ticks in production; these tests drive `tick()` directly with a shared `cache`, the way
    `test_a_persistent_failure_warns_once_and_then_goes_quiet` already does for step 8b."""

    def test_hourly_fields_absent_when_the_gate_has_not_elapsed(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "health_read_at": T0}
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=30))
        point = cache["publisher"].points[0]
        assert "fault_raw_0131" not in point

    def test_hourly_fields_present_once_the_gate_has_elapsed(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "health_read_at": T0 - dt.timedelta(hours=2)}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        point = cache["publisher"].points[0]
        assert "fault_raw_0131" in point

    def test_hourly_fields_do_not_republish_on_the_tick_right_after_crossing(
            self, tmp_path, monkeypatch):
        """The gate, once crossed, must not fire again a minute later -- the whole point of
        HEALTH_REFRESH_S existing."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub,
                      "health_read_at": T0 - dt.timedelta(hours=2)}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert "fault_raw_0131" in pub.points[0]

        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=1))
        assert "fault_raw_0131" not in pub.points[1]

    def test_weekly_fields_absent_when_the_gate_has_not_elapsed(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "weekly_firmware_read_at": T0}
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(days=1))
        point = cache["publisher"].points[0]
        assert "firmware_raw_0115" not in point

    def test_weekly_fields_present_once_the_gate_has_elapsed(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "weekly_firmware_read_at": T0 - dt.timedelta(days=8)}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        point = cache["publisher"].points[0]
        assert "firmware_raw_0115" in point

    def test_weekly_fields_do_not_republish_on_the_tick_right_after_crossing(
            self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub,
                      "weekly_firmware_read_at": T0 - dt.timedelta(days=8)}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert "firmware_raw_0115" in pub.points[0]

        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=1))
        assert "firmware_raw_0115" not in pub.points[1]


class TestDailyHealthTier:
    """Step 8e: SoH, the three lifetime energy counters, lifetime PV, the heatsink.

    The gate mechanics are shared with the weekly tier (`_read_weekly_block`), so what is
    tested here is what the daily tier adds: a plausibility guard on every block, and three
    independent gates over three unrelated register ranges.
    """

    def test_the_confirmed_values_reach_the_point(self, tmp_path, monkeypatch):
        """Across a cold start's first few ticks -- one slow block per tick, see
        `settle_slow_blocks`. The values are the ones the live inverter returned on
        2026-09-03."""
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        merged = {k: v for point in pub.points for k, v in point.items()}
        assert merged["soh_pct"] == 100.0
        assert merged["lifetime_charge_kwh"] == 1048.1
        assert merged["lifetime_discharge_kwh"] == 1022.1
        assert merged["lifetime_grid_charge_kwh"] == 581.1
        assert merged["inverter_temp_c"] == 37.0
        assert merged["lifetime_pv_kwh"] == 867.11

    def test_absent_when_the_gate_has_not_elapsed(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "daily_battery_read_at": T0}
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(hours=6))
        assert "soh_pct" not in cache["publisher"].points[0]

    def test_present_once_the_gate_has_elapsed(self, tmp_path, monkeypatch):
        """Every other slow block is marked freshly read, so the tick's one slow read is
        unambiguously this one -- otherwise the assertion would be about read ORDER, which is
        not something this tier promises."""
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher(),
                      "daily_battery_read_at": T0 - dt.timedelta(days=2),
                      **others_recently_read("daily_battery")}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert "soh_pct" in cache["publisher"].points[0]

    def test_does_not_republish_on_the_tick_right_after_crossing(self, tmp_path, monkeypatch):
        client = ScriptedClient(measurement_registers())
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub,
                      "daily_battery_read_at": T0 - dt.timedelta(days=2),
                      **others_recently_read("daily_battery")}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert "soh_pct" in pub.points[0]

        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(minutes=1))
        assert "soh_pct" not in pub.points[1]

    def test_an_implausible_block_publishes_nothing_rather_than_a_number(
            self, tmp_path, monkeypatch, caplog):
        """The case this tier's guards exist for, and the reason they raise ValueError into the
        shared gate rather than returning a value: an all-zero SoH/energy block is in range at
        every scale, and a dashboard reading 0.0 kWh lifetime charge is a wrong number nobody
        has reason to doubt. No field is the honest report.
        """
        regs = measurement_registers()
        for addr in (R.REG_SOH, R.REG_LIFETIME_CHARGE, R.REG_LIFETIME_CHARGE + 1,
                     R.REG_LIFETIME_DISCHARGE, R.REG_LIFETIME_DISCHARGE + 1,
                     R.REG_LIFETIME_GRID_CHARGE, R.REG_LIFETIME_GRID_CHARGE + 1):
            regs[addr] = 0
        client = ScriptedClient(regs)
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        with caplog.at_level("WARNING"):
            settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        assert "soh_pct" not in seen
        assert "lifetime_charge_kwh" not in seen
        assert "implausible" in caplog.text
        # Reported as a READ THAT CAME BACK UNUSABLE, not as a read that failed: the register
        # answered perfectly well, and a log line blaming the network sends whoever is
        # debugging it somewhere there is nothing to find.
        assert "read back an unusable value" in caplog.text
        assert "battery block read failed" not in caplog.text
        # The heatsink and PV blocks are untouched by that failure -- see the next test.
        assert "inverter_temp_c" in seen

    def test_an_implausible_block_retries_hourly_rather_than_waiting_a_day(
            self, tmp_path, monkeypatch):
        """A rejected read takes the same backoff as an unreadable one. A transient bad block
        must not cost a full day before the next attempt, which is the same argument
        `_read_weekly_block` already makes for the weekly tier."""
        regs = measurement_registers()
        regs[R.REG_SOH] = 0
        client = ScriptedClient(regs)
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub,
                      **others_recently_read("daily_battery")}
        tick_with_cache(tmp_path, monkeypatch, client, cache, now=T0)
        assert cache["daily_battery_fail_streak"] == 1
        assert "soh_pct" not in pub.points[0]

        # Six hours later the register answers properly: the field appears, well inside the
        # day the gate would otherwise have imposed.
        client.registers[R.REG_SOH] = 1000
        tick_with_cache(tmp_path, monkeypatch, client, cache,
                        now=T0 + dt.timedelta(hours=6))
        assert pub.points[1]["soh_pct"] == 100.0
        assert cache["daily_battery_fail_streak"] == 0

    def test_the_three_blocks_have_independent_gates(self, tmp_path, monkeypatch):
        """Three unrelated register ranges with independent support. 0x08D0 in particular
        appears in no document this repo has, so it is the likeliest of the three to be
        unsupported on other firmware -- and it must not be able to take SoH down with it."""
        regs = measurement_registers()
        regs[R.REG_LIFETIME_PV] = 0
        regs[R.REG_LIFETIME_PV + 1] = 0
        client = ScriptedClient(regs)
        pub = RecordingPublisher()
        cache: dict = {"released": False, "publisher": pub}
        settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        seen = fields_seen(pub)
        assert "lifetime_pv_kwh" not in seen
        assert "soh_pct" in seen
        assert "inverter_temp_c" in seen
        # BOTH HALVES, not just the survivors: a decode that quietly returned None instead of
        # raising would leave every sibling healthy too, and this test would pass while the
        # guard did nothing.
        assert cache["daily_pv_fail_streak"] == 1
        assert cache["daily_battery_fail_streak"] == 0
        assert cache["daily_inverter_fail_streak"] == 0

    def test_the_daily_and_weekly_gates_cannot_collide(self, tmp_path, monkeypatch):
        """`_read_weekly_block` keys its cache entries by tier prefix as well as block name.
        Both tiers have a block that could reasonably be called the same thing, and a shared
        key would silently make one tier's read satisfy the other's gate."""
        client = ScriptedClient(measurement_registers())
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        settle_slow_blocks(tmp_path, monkeypatch, client, cache)
        assert "daily_battery_read_at" in cache
        assert "weekly_firmware_read_at" in cache
        assert cache["daily_battery_read_at"] is not None

    def test_skipped_entirely_when_the_tick_already_has_a_read_error(
            self, tmp_path, monkeypatch):
        """Same reasoning as 8c and 8d: three more block reads against an inverter that has
        already failed to answer buys three more ~12 s timeouts and delays the heartbeat."""
        client = ScriptedClient(measurement_registers(),
                                fail_addrs=frozenset({R.REG_START}))
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        tick_with_cache(tmp_path, monkeypatch, client, cache)
        assert "soh_pct" not in cache["publisher"].points[0]
        assert "daily_battery_read_at" not in cache


class TestMagnitudeShortfall:
    """`slots.SHORTFALL_PCT` / `SHORTFALL_MIN_W`, checked against the PREVIOUS tick's command
    -- `batt_w` read this tick reflects up to a tick's worth of settling under
    `cache["last_written"]`, not the command this tick is about to issue."""

    def _two_ticks(self, tmp_path, monkeypatch, battery_power_w, dry_run=False):
        slots_path = tmp_path / "slots.json"
        slots_path.write_text(json.dumps(doc()))
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "hb.json")
        client = ScriptedClient(measurement_registers(battery_power_w=battery_power_w))
        inv = scheduler.Inverter(client, 0x55, dry_run=dry_run)
        cache: dict = {"released": False, "publisher": RecordingPublisher()}
        # First tick establishes `cache["last_written"]` -- doc()'s slot commands 4,500 W
        # discharge. The reading this same tick sees is whatever the house was doing before
        # any command existed, so only the SECOND tick's comparison means anything.
        asyncio.run(scheduler.tick(inv, slots_path, cache, T0))
        asyncio.run(scheduler.tick(inv, slots_path, cache, T0 + dt.timedelta(seconds=60)))
        return cache

    def test_a_large_shortfall_is_logged_once_on_entry(self, tmp_path, monkeypatch, caplog):
        # 4,500 W commanded, 4,000 W delivered: 500 W and 11% short, over both thresholds.
        with caplog.at_level("WARNING", logger="dispatch"):
            cache = self._two_ticks(tmp_path, monkeypatch, battery_power_w=4000)
        assert cache["shorted"] is True
        shortfalls = [r for r in caplog.records if "magnitude shortfall" in r.message]
        assert len(shortfalls) == 1, "should log once on entry, not once per tick"

    def test_delivery_within_tolerance_is_not_flagged(self, tmp_path, monkeypatch, caplog):
        # 4,500 commanded, 4,400 delivered: 100 W and 2.2% short -- under SHORTFALL_PCT (5%).
        with caplog.at_level("WARNING", logger="dispatch"):
            cache = self._two_ticks(tmp_path, monkeypatch, battery_power_w=4400)
        assert cache["shorted"] is False
        assert not any("magnitude shortfall" in r.message for r in caplog.records)

    def test_dry_run_never_flags_a_shortfall(self, tmp_path, monkeypatch, caplog):
        """`cache['last_written']` is set in dry run too, from a command `Inverter.write`
        never actually sent -- comparing it to real battery power would score ordinary
        self-consumption against a command nobody issued."""
        with caplog.at_level("WARNING", logger="dispatch"):
            cache = self._two_ticks(tmp_path, monkeypatch, battery_power_w=0, dry_run=True)
        assert cache["shorted"] is False
        assert not any("magnitude shortfall" in r.message for r in caplog.records)


class TestLoopPacing:
    """`next_deadline` is `run()`'s clock, extracted so it can be tested without a loop.

    The bug it fixes is silent by construction: the dead man's switch is 5x the interval, so
    a loop running slow keeps the battery under control and nothing goes red. It was found
    only by reading timestamps in a log opened for a different reason -- one unroutable
    heartbeat URL spending 5 s in `urlopen` every tick, and a control loop quietly running at
    65 s instead of 60.
    """

    INTERVAL = 60.0

    def test_the_period_is_the_interval_not_the_interval_plus_the_work(self):
        """The whole point. A tick that starts at its deadline and takes 5 s must leave the
        next one due 60 s after the FIRST started, not 60 s after it finished."""
        assert scheduler.next_deadline(1000.0, 1005.0, self.INTERVAL) == 1060.0

    def test_a_slow_tick_does_not_shorten_the_next_sleep_below_zero(self):
        """A tick that overruns lands past its own next deadline. The loop must not be handed
        a deadline in the past and fire immediately -- `run()` clamps the wait at 0, and this
        pins the other half: the deadline itself moves forward."""
        assert scheduler.next_deadline(1000.0, 1075.0, self.INTERVAL) > 1075.0

    def test_missed_intervals_are_skipped_not_replayed(self):
        """NO BURST CATCH-UP. Replaying a backlog would fire one Modbus write per skipped
        interval, back-to-back, at the inverter that was just too slow to answer -- the
        failure feeding itself. One tick instead, at the next phase boundary, and strictly
        the NEXT one, so a tick landing exactly on a boundary sleeps a full interval rather
        than firing on a zero-length wait.

        The 290 s example is deliberately past the failsafe margin: by then the command has
        already expired (see the test below), so the choice is between one write to a
        released battery and five. It is not the case this is tuned for -- that is a tick or
        two of slippage -- but it is the case that decides the design."""
        assert scheduler.next_deadline(1000.0, 1290.0, self.INTERVAL) == 1300.0
        assert scheduler.next_deadline(1000.0, 1300.0, self.INTERVAL) == 1360.0

    def test_the_phase_survives_a_skip(self):
        """Deadlines stay on the original grid, so a single slow tick does not permanently
        re-time the loop onto a new offset. Every deadline this returns is a whole number of
        intervals from where the loop started."""
        for now in (1000.0, 1030.0, 1061.0, 1119.9, 1500.0, 1e6):
            out = scheduler.next_deadline(1000.0, now, self.INTERVAL)
            assert (out - 1000.0) % self.INTERVAL == 0, now
            assert out > now

    def test_a_tick_finishing_early_still_waits(self):
        """The common case, and the one a naive `max(0, ...)` rewrite would break: a fast
        tick must leave the deadline where it was rather than pulling it forward."""
        assert scheduler.next_deadline(1000.0, 1000.5, self.INTERVAL) == 1060.0

    def test_the_failsafe_margin_is_three_missed_ticks_not_four(self):
        """The 5x ratio between the two constants reads like four missed ticks. It is three,
        and skipping rather than bursting is what spends that margin, so the number wants
        pinning rather than restating.

        Every commanding tick re-arms the switch, so a command written at t0 has its next
        write due at t0 + (missed+1)*interval. Three misses land at t0+240 with a minute in
        hand; four land at t0+300, which is expiry itself -- and later than that in practice,
        because the write ends a tick that reads the inverter first. `>=` is deliberate on
        the second assertion: landing exactly on the expiry instant is already a loss.
        """
        import slots as S
        survivable = 3
        assert (survivable + 1) * S.REFRESH_INTERVAL_S < S.DISPATCH_DURATION_S, (
            "three missed ticks no longer fit inside the dead man's switch")
        assert (survivable + 2) * S.REFRESH_INTERVAL_S >= S.DISPATCH_DURATION_S, (
            "the margin grew past three missed ticks -- the comments in slots.py, "
            "scheduler.next_deadline and docs/DISPATCH-FLOW.md all state three")
