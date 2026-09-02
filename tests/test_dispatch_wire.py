"""Command -> wire. Layer C of PLAN-repo-seams.md section 5d.

The only layer where a simulator earns its keep. Layers A and B are pure functions and need
nothing; here the question is what actually lands on the bus, and that is where the
`0x0883`/`0x0885` class of error lives -- a mistake no unit test on an encoder can catch,
because the encoder is perfectly happy writing the right value to the wrong address.

Runs pymodbus's own TCP server in-process against a scratch register file, so it exercises
the real client, the real framing and the real addresses. It does NOT prove anything about
the inverter's behaviour -- only that we speak the protocol we think we speak.

THE ONE THING THE HARDWARE ADDS that this cannot: the real inverter accepts exactly one TCP
connection. The simulator happily accepts many, so a second-connection bug would pass here
and fail on the NAS.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket

import pytest

pytest.importorskip("pymodbus", reason="pymodbus is not installed")

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import SimData, SimDevice
from pymodbus.simulator.simdata import DataType

import registers as R
import scheduler
import slots as S

SLAVE = 0x55
UTC = dt.UTC


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def on_simulator(body, seed: dict[int, list[int]] | None = None):
    """Run `body(inverter, trace)` against an in-process Modbus server.

    `trace` accumulates the addresses of every write the server RECEIVED, in order -- which
    is how the write-ordering safety property is tested rather than asserted in a comment.
    """
    async def main():
        port = _free_port()
        trace: list[tuple[int, int]] = []

        def trace_pdu(sending, pdu):
            if not sending and getattr(pdu, "function_code", None) in (6, 16):
                trace.append((pdu.function_code, pdu.address))
            return pdu

        device = SimDevice(
            SLAVE, simdata=[SimData(0, count=3000, datatype=DataType.REGISTERS)])
        server = ModbusTcpServer(
            device, address=("127.0.0.1", port), trace_pdu=trace_pdu)
        serving = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.3)

        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        await client.connect()
        assert client.connected, "simulator did not accept a connection"
        try:
            if seed:
                setup = scheduler.Inverter(client, SLAVE, dry_run=False)
                for addr, words in seed.items():
                    await setup.write(addr, words)
                trace.clear()
            return await body(scheduler.Inverter(client, SLAVE, dry_run=False), trace)
        finally:
            client.close()
            await server.shutdown()
            serving.cancel()

    return asyncio.run(main())


class TestCommandOnTheWire:
    def test_a_soc_command_round_trips(self):
        async def body(inv, _trace):
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            return await inv.read_block()

        state = on_simulator(body)
        assert state == {
            "dispatch_active": 1, "mode": 2, "mode_name": "SoC control",
            "power_w": -4500, "target_soc_pct": 20.0, "duration_s": 300,
        }

    def test_a_charge_keeps_its_sign_across_the_wire(self):
        """Charging-positive here, discharge-positive on the bus, offset by +32000. Three
        conventions in one value; this is the end-to-end check that they compose."""
        async def body(inv, _trace):
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, 4000, 90.0, 300))
            return await inv.read_block()

        assert on_simulator(body)["power_w"] == 4000

    def test_mode_lands_at_0x0885_and_reactive_power_is_never_touched(self):
        """The error this whole layer exists for.

        0x0883 is reactive power and sits two addresses below the mode register. Writing the
        mode there would be accepted by any encoder and by the bus, and would command
        reactive power on a real inverter while the mode stayed whatever it was.
        """
        async def body(inv, _trace):
            await inv.apply(R.Command(R.DispatchMode.FOLLOW, 0, None, 300))
            return await inv.read(0x0885), await inv.read(0x0883, count=2)

        mode, reactive = on_simulator(body, seed={0x0883: [0, 0]})
        assert mode == 3
        assert reactive == 0

    def test_start_is_written_last(self):
        """The safety property, observed rather than asserted in a comment.

        Until START is set a partially-written command is inert. Writing START first would
        briefly run the PREVIOUS command's power and mode against the new duration.
        """
        async def body(inv, trace):
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            return list(trace)

        writes = on_simulator(body)
        addresses = [addr for _fc, addr in writes]
        assert addresses[-1] == R.REG_START
        assert R.REG_START not in addresses[:-1]
        assert set(addresses[:-1]) == {R.REG_MODE, R.REG_POWER, R.REG_SOC, R.REG_TIME}

    def test_a_hold_never_writes_the_soc_register(self):
        """Section 5 step 6. A stale target left behind would be misread by the next reader
        -- including the dashboard's decode table."""
        async def body(inv, trace):
            await inv.apply(R.Command(R.DispatchMode.FOLLOW, 0, None, 300))
            return [addr for _fc, addr in trace], await inv.read(R.REG_SOC)

        addresses, soc_raw = on_simulator(body, seed={R.REG_SOC: [222]})
        assert R.REG_SOC not in addresses
        assert soc_raw == 222, "the sentinel was overwritten -- a hold wrote an SoC target"

    def test_release_clears_start_and_deliberately_leaves_the_payload(self):
        """Release is `start=0`, not a wipe. The payload staying put is what makes the
        dashboard's decode table still meaningful after a release -- and it is why
        `dispatch_active` is the only field that says whether a command is live.
        """
        async def body(inv, _trace):
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            await inv.release()
            return await inv.read_block()

        state = on_simulator(body)
        assert state["dispatch_active"] == 0
        assert state["power_w"] == -4500
        assert state["mode"] == 2

    def test_dry_run_reads_but_never_writes(self):
        """The default. `--live` is required before anything reaches a register."""
        async def body(inv, trace):
            inv.dry_run = True
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            return list(trace), await inv.read_block()

        writes, state = on_simulator(body)
        assert writes == []
        assert state["dispatch_active"] == 0


class TestLimits:
    def test_limits_are_read_from_the_inverter(self):
        async def body(inv, _trace):
            return await inv.limits()

        assert on_simulator(body, seed={R.REG_MAX_CHARGE_POWER: [5000],
                                        R.REG_MAX_DISCHARGE_POWER: [4900]}) == (5000, 4900)


class TestTickEndToEnd:
    """`tick()` with a real slots.json, a real clock value and a real bus. The last thing
    between here and hardware."""

    def _slots_file(self, tmp_path, now, action="discharge"):
        slot = {"start": (now - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": (now + dt.timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "action": action}
        if action in ("charge", "discharge"):
            slot |= {"power_w": 4500, "target_soc": 20.0}
        doc = {
            "generated_at": (now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plan_run": "2026-08-01T09:00:00Z",
            "horizon_end": (now + dt.timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval_minutes": 15, "capacity_wh": 27900.0, "slots": [slot],
        }
        p = tmp_path / "slots.json"
        p.write_text(json.dumps(doc))
        return p

    @pytest.fixture(autouse=True)
    def _heartbeat(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scheduler, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")

    def test_a_planned_discharge_reaches_the_registers(self, tmp_path):
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            decision = await scheduler.tick(inv, path, {}, now)
            return decision, await inv.read_block()

        # Live SoC 80.0 % -- comfortably above the 20 % target, so the direction rule passes.
        decision, state = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert decision.kind == "command"
        assert state["dispatch_active"] == 1
        assert state["mode"] == 2
        assert state["power_w"] == -4500
        assert state["target_soc_pct"] == 20.0
        assert state["duration_s"] == S.DISPATCH_DURATION_S

    def test_a_half_applied_readback_does_not_alarm_on_the_first_tick(self, tmp_path, caplog):
        """The mode register is cleared and reloaded when START goes 0 -> 1, so the first
        command tick after a release reads mode=0 beside a power, target and duration that
        already hold what was written. Measured 2026-09-01T14:15:50Z and, on the discharge
        side, 2026-08-26T16:15:12Z; 36 times in ~12,000 writes and never twice in a row.

        The tick still publishes `verified=0` -- that is what the block held when it was
        asked -- but the ERROR line and monitor #6 wait for a second consecutive failure.
        Monitor #6 means "the inverter is refusing our writes", and it can only keep meaning
        that if it is not also said three times a day."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            real = inv.read_raw_block
            calls = []

            async def flaky():
                words = await real()
                calls.append(1)
                if len(calls) == 2:          # 1 = the hijack read, 2 = the verify read
                    words = list(words)
                    words[5] = 0                             # mode not reloaded yet
                return words

            inv.read_raw_block = flaky
            cache = {}
            with caplog.at_level("WARNING", logger="dispatch"):
                await scheduler.tick(inv, path, cache, now)
            return cache, len(calls), [(r.levelname, r.message) for r in caplog.records]

        cache, reads, records = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert cache["write_verified"] is False
        assert cache["unverified_streak"] == 1
        assert reads == 2, "no in-tick re-read: it never once rescued a real one"
        assert not [m for lvl, m in records if lvl == "ERROR"]
        assert any("rechecking next tick" in m for _, m in records)

    def test_a_block_that_stays_wrong_still_fails_verification(self, tmp_path, monkeypatch):
        """The debounce must not turn monitor #6 off. A block that never takes the write is
        an inverter refusing it, and that is the failure this design fears most."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            real = inv.read_raw_block

            async def wrong():
                words = list(await real())
                words[5] = 0
                return words

            inv.read_raw_block = wrong
            cache = {}
            await scheduler.tick(inv, path, cache, now)
            return cache

        cache = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert cache["write_verified"] is False

    def test_a_self_slot_releases_dispatch(self, tmp_path):
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now, action="self")

        async def body(inv, _trace):
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            decision = await scheduler.tick(inv, path, {}, now)
            return decision, await inv.read_block()

        decision, state = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert decision.kind == "release"
        assert state["dispatch_active"] == 0

    def test_the_direction_rule_downgrades_against_live_soc(self, tmp_path):
        """Live SoC below the discharge target -- the command would be a silent no-op, so
        the loop holds instead."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            decision = await scheduler.tick(inv, path, {}, now)
            return decision, await inv.read_block()

        decision, state = on_simulator(body, seed={R.REG_BATTERY_SOC: [150]})  # 15.0 %
        assert decision.kind == "command"
        assert state["mode"] == R.DispatchMode.FOLLOW
        assert state["power_w"] == 0

    def test_a_stale_plan_releases_once_then_writes_nothing(self, tmp_path):
        """The documented departure from section 5: idle releases on the way in rather than
        leaving a stale command running for its full 300 s, then goes silent."""
        now = dt.datetime.now(UTC)
        stale = self._slots_file(tmp_path, now - dt.timedelta(hours=9))

        async def body(inv, trace):
            cache: dict = {}
            first = await scheduler.tick(inv, stale, cache, now)
            after_first = [addr for _fc, addr in trace]
            trace.clear()
            second = await scheduler.tick(inv, stale, cache, now)
            return first, second, after_first, [addr for _fc, addr in trace]

        first, second, first_writes, second_writes = on_simulator(
            body, seed={R.REG_BATTERY_SOC: [800]})
        assert first.kind == second.kind == "idle"
        assert first_writes == [R.REG_START]
        assert second_writes == [], "idle must be silent after the first release"

    def test_the_heartbeat_is_written_every_tick(self, tmp_path):
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            return await scheduler.tick(inv, path, {}, now)

        on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        payload = json.loads(scheduler.HEARTBEAT_PATH.read_text())
        assert payload["kind"] == "command"
        assert payload["live_soc_pct"] == 80.0
        assert payload["readback"]["dispatch_active"] in (0, 1)
        assert payload["slot"]["action"] == "discharge"

    def test_alive_reads_the_heartbeat_without_touching_modbus(self, tmp_path, capsys):
        """`--alive` must never open a connection -- it would steal the inverter's single
        slot from the very process it is checking."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            return await scheduler.tick(inv, path, {}, now)

        on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert scheduler.check_alive() == 0
        assert "ALIVE" in capsys.readouterr().out

    def test_alive_reports_missing_when_the_loop_has_never_run(self, capsys):
        assert scheduler.check_alive() == 2
        assert "never run" in capsys.readouterr().out

    def test_a_missing_slots_file_is_idle_not_a_crash(self, tmp_path):
        now = dt.datetime.now(UTC)

        async def body(inv, _trace):
            return await scheduler.tick(inv, tmp_path / "gone.json", {}, now)

        decision = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert decision.kind == "idle"
        assert "does not exist" in decision.reason

    def test_the_published_state_reflects_the_write_not_the_previous_command(self, tmp_path):
        """The reason there is a SECOND readback.

        The hijack-check read happens before we write, so it necessarily holds the PREVIOUS
        command. Publishing that would lag the dashboard a tick and show a command as live
        before it had landed.
        """
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)
        published: list[dict] = []

        class Recorder:
            def publish(self, fields, now=None):
                published.append(fields)
                return True

        async def body(inv, _trace):
            # Pre-load a DIFFERENT command, so a stale readback would be obvious.
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, 3000, 95.0, 300))
            return await scheduler.tick(inv, path, {"publisher": Recorder()}, now)

        on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert len(published) == 1
        fields = published[0]
        assert fields["setpoint_w"] == -4500, "published the previous command, not the new one"
        assert fields["target_soc_pct"] == 20.0
        assert fields["action"] == "discharging to grid"
        assert fields["raw_0885"] == 2
        assert fields["slot_action"] == "discharge"
        assert fields["plan_run"] == "2026-08-01T09:00:00Z"

    def test_the_release_on_the_way_into_idle_is_published(self, tmp_path):
        """The idle path writes too -- it releases once on the way in. Skipping the second
        readback there published the block as it stood BEFORE that release, so the dashboard
        showed a live command on the one tick where the loop had just given up."""
        now = dt.datetime.now(UTC)
        stale = self._slots_file(tmp_path, now - dt.timedelta(hours=9))
        published: list[dict] = []

        class Recorder:
            def publish(self, fields, now=None):
                published.append(fields)
                return True

        async def body(inv, _trace):
            # A command is live when the plan goes stale -- exactly the real sequence.
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, -4500, 20.0, 300))
            return await scheduler.tick(inv, stale, {"publisher": Recorder()}, now)

        decision = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert decision.kind == "idle"
        assert published[0]["dispatch_active"] == 0, "published the pre-release readback"
        assert published[0]["raw_0880"] == 0

    def test_a_verified_write_is_recorded_as_verified(self, tmp_path):
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            cache: dict = {}
            await scheduler.tick(inv, path, cache, now)
            return cache["write_verified"]

        assert on_simulator(body, seed={R.REG_BATTERY_SOC: [800]}) is True

    def test_a_write_that_does_not_land_is_caught(self, tmp_path, caplog):
        """Monitor #6. The failure this design fears most: every log line says "commanded",
        the battery does nothing, and nothing notices."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            real_apply = inv.apply

            async def apply_then_sabotage(cmd):
                await real_apply(cmd)
                # Something else moves the register between our write and our verify.
                await inv.write(R.REG_MODE, [1])

            inv.apply = apply_then_sabotage
            cache: dict = {}
            with caplog.at_level("ERROR", logger="dispatch"):
                # TWO ticks, because one is the mode register settling after a release and
                # that reading is not evidence of anything. The sabotage here is permanent,
                # so the second tick is what makes it a real failure.
                await scheduler.tick(inv, path, cache, now)
                first = [r.message for r in caplog.records]
                await scheduler.tick(inv, path, cache, now)
            return (cache["write_verified"], cache["unverified_streak"],
                    first, [r.message for r in caplog.records])

        verified, streak, first, messages = on_simulator(
            body, seed={R.REG_BATTERY_SOC: [800]})
        assert verified is False
        assert streak == 2
        assert not [m for m in first if "WRITE NOT VERIFIED" in m], (
            "the first unverified tick must not raise the alarm")
        assert any("WRITE NOT VERIFIED" in m for m in messages)

    def test_surplus_is_read_as_negative_grid_plus_battery(self, tmp_path):
        """The sign convention that makes the whole feature safe: `surplus = -(grid + battery)`
        is invariant to what the battery is doing, unlike the grid meter alone. Verified
        against the collector's independent cloud reading at 2026-08-20T14:27:24Z: grid
        +3,099 W (importing), battery -4,795 W (charging), surplus -(3099 - 4795) = 1,696 W."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now, action="hold")

        async def body(inv, _trace):
            return await scheduler.tick(inv, path, {}, now)

        decision = on_simulator(body, seed={
            R.REG_BATTERY_SOC: [800],
            R.REG_GRID_POWER: R.encode_int32(3099),
            R.REG_BATTERY_POWER: [(-4795) & 0xFFFF],
        })
        payload = json.loads(scheduler.HEARTBEAT_PATH.read_text())
        assert payload["surplus_w"] == 1696
        # 1,696 W is above SURPLUS_HARVEST_W, so the plan's own hold releases to harvest it.
        assert decision.kind == "release"

    def test_an_implausible_reading_falls_back_to_no_surplus(self, tmp_path):
        """A decode error on a register neither read by this process before 2026-08-20 must
        not masquerade as a real value -- IMPLAUSIBLE_POWER_W catches it and the tick falls
        back to the pre-existing freeze rather than trusting a nonsense surplus."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now, action="hold")

        async def body(inv, _trace):
            return await scheduler.tick(inv, path, {}, now)

        decision = on_simulator(body, seed={
            R.REG_BATTERY_SOC: [800],
            R.REG_GRID_POWER: R.encode_int32(50000),  # past scheduler.IMPLAUSIBLE_POWER_W
            R.REG_BATTERY_POWER: [0],
        })
        payload = json.loads(scheduler.HEARTBEAT_PATH.read_text())
        assert payload["surplus_w"] is None
        assert decision.kind == "command", "no surplus reading means the hold must stand"

    def test_an_unconfirmed_write_does_not_trigger_hijack_next_tick(self, tmp_path,
                                                                     monkeypatch):
        """OBSERVED 2026-08-20 17:34:58Z-17:35:59Z: a hold write failed verification (mode
        never left 0), and the NEXT tick's hijack check compared the still-stuck block
        against `last_written` and reported "the app is dispatching" -- when the actual
        explanation was still our own write from a tick earlier, already alarmed on by
        monitor #6 (WRITE NOT VERIFIED). A write that never verifies must not ALSO read as
        a hijack next tick."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            real_apply = inv.apply

            async def apply_then_stick_wrong_mode(cmd):
                # Something keeps resetting the mode register behind every write this
                # process makes -- mode never lands as commanded, on either tick.
                await real_apply(cmd)
                await inv.write(R.REG_MODE, [0])

            inv.apply = apply_then_stick_wrong_mode
            cache: dict = {}
            await scheduler.tick(inv, path, cache, now)
            assert cache["write_verified"] is False, "the setup must reproduce the failure"
            await scheduler.tick(inv, path, cache, now)
            return cache["hijacked"]

        hijacked = on_simulator(body, seed={R.REG_BATTERY_SOC: [800]})
        assert not hijacked, (
            "our own unverified write from the previous tick must not read as the app "
            "dispatching")

    def test_a_hijack_is_detected_and_recorded(self, tmp_path):
        """The app writing the same registers. Detected before we overwrite the evidence."""
        now = dt.datetime.now(UTC)
        path = self._slots_file(tmp_path, now)

        async def body(inv, _trace):
            # The app's 2026-08-15 signature, already live before our first tick.
            await inv.apply(R.Command(R.DispatchMode.SOC_TARGET, 5000, 100.0, 5580))
            cache: dict = {}
            await scheduler.tick(inv, path, cache, now)
            return cache["hijacked"]

        assert on_simulator(body, seed={R.REG_BATTERY_SOC: [800]}) is True
