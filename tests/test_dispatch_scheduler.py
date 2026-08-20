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
