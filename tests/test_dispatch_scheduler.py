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
    return regs


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
        # the verify readback (and its one retry) keeps reporting the pre-write, released
        # state -- exactly what "the block does not hold what was just written" looks like.
        monkeypatch.setattr(scheduler, "VERIFY_RETRY_DELAY_S", 0)
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
