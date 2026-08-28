"""The 60 s control loop. DESIGN-dispatch.md section 5.

Thin on purpose. Every decision lives in `slots.decide()`, which is pure; this module owns
only the things that cannot be tested without hardware -- the Modbus connection, the clock,
the heartbeat file and the audit log. If logic starts accumulating here, it belongs in
slots.py where it can be tested at a boundary instant instead of at 60 s per case.

THE ONE CONSTRAINT: the inverter accepts exactly ONE Modbus TCP connection (a second returns
Errno 61). This process holds it for its whole life. Nothing else may talk to :502 while this
runs -- not `--status`, not a Kuma port monitor, not a second replica. That is why this is a
single compose service with no scaling and a restart policy.

FAIL-SAFE: silence. Every path that cannot decide confidently stops writing and lets the
inverter's own dead man's switch revert it to self-consumption. That is only actually safe
once the AlphaESS app's price-based control is off -- see section 8.

Usage:
    python scheduler.py --ip 192.168.68.151 --slots /data/slots.json          # dry run
    python scheduler.py --ip 192.168.68.151 --slots /data/slots.json --live
    python scheduler.py --alive                                               # no Modbus
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import inspect
import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

import registers as R
import slots as S
import state as state_mod
from heartbeat import send_heartbeat

try:
    from pymodbus.client import AsyncModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:  # --alive must work without pymodbus installed
    AsyncModbusTcpClient = None
    # An empty tuple never matches in `except`, which is what we want: with pymodbus absent
    # nothing here can reach the bus, so there is no Modbus failure left to catch.
    ModbusException = ()

log = logging.getLogger("dispatch")

HEARTBEAT_PATH = Path(os.environ.get("DISPATCH_HEARTBEAT", "dispatch_heartbeat.json"))

# The five Kuma monitors this loop is responsible for -- section 6.1's #4-#8. #1-#3 are pinged
# elsewhere (#1 in `battery-planning`, #2 and #3 by the translator) and #9 is a daily job.
#
# Read once at import and keyed by monitor name so the mapping between "the table in section
# 6.1" and "the env var in docker-compose.yml" is one dict rather than five scattered lookups.
# Unset is the documented "not monitored yet" state: monitors get created during go-live, and
# the loop has to run before that. An unset URL makes `send_heartbeat` a no-op.
MONITOR_URLS = {
    "slots-fresh": os.environ.get("SLOTS_FRESH_HEARTBEAT_URL", ""),
    "dispatcher-alive": os.environ.get("DISPATCHER_ALIVE_HEARTBEAT_URL", ""),
    "dispatch-confirmed": os.environ.get("DISPATCH_CONFIRMED_HEARTBEAT_URL", ""),
    "inverter-not-hijacked": os.environ.get("INVERTER_NOT_HIJACKED_HEARTBEAT_URL", ""),
    "soc-floor": os.environ.get("SOC_FLOOR_HEARTBEAT_URL", ""),
}

# The inverter's own limit registers are re-read on this cadence, not just at startup.
#
# They are not constants: 0x012C/0x012D were measured at 15,015/13,728 W on 2026-08-16 and
# 15,592/15,645 W later the same day. A value read at container start and held for weeks is a
# clamp against a number the hardware has since moved away from -- and the case the clamp is
# there for, a derate, is exactly the case where the number changes mid-run.
LIMITS_REFRESH_S = 3600

# The health-poller's own cadence gates, unrelated to LIMITS_REFRESH_S above: that one is a
# clamping concern (`run()`'s loop, feeding `S.clamp`), these are observability, gated inside
# `tick()` itself -- see step 8c/8d for why they live there rather than in `run()`.
HEALTH_REFRESH_S = 3600
WEEKLY_HEALTH_REFRESH_S = 604800

# How many consecutive HEALTH_REFRESH_S-spaced failures a single weekly block tolerates before
# giving up on the hourly backoff and reverting to the full week. Three hours of a register
# that never answers is enough to call it unsupported rather than transient -- past that,
# retrying it hourly forever is the same Modbus-budget risk `read_error` skipping (8c/8d)
# exists to bound, just paid weekly instead of every tick.
WEEKLY_FAIL_STREAK_LIMIT = 3

# A house that is neither generating nor drawing more than this. Anything past it from the
# measurement registers is a decode error, not a reading: the site is a 5 kW inverter behind a
# 3x25 A connection.
IMPLAUSIBLE_POWER_W = 30000

# How long to wait before re-reading a dispatch block that did not verify. Long enough for the
# inverter to finish ingesting a block written milliseconds earlier, short enough to be
# invisible inside a 60 s loop -- and it is only ever paid on a tick that was about to raise an
# alarm anyway.
VERIFY_RETRY_DELAY_S = 0.5


def _id_kwarg() -> str:
    """pymodbus renamed slave= to device_id=. Detected rather than pinned, because the
    scripts here are run from several environments (the NAS image, a laptop venv, CI)."""
    params = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
    return "device_id" if "device_id" in params else "slave"


class Inverter:
    def __init__(self, client, slave_id: int, dry_run: bool):
        self.c, self.slave, self.dry_run = client, slave_id, dry_run
        self.kw = _id_kwarg()

    async def read(self, addr: int, count: int = 1, signed: bool = False) -> int:
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:
            # WHY THIS CONVERSION EXISTS, and it is not tidiness.
            #
            # Every caller in this file degrades on `except OSError` -- read the SoC, fail,
            # decide without it; read the block, fail, skip the hijack check. Those handlers
            # were dead code for two years' worth of the failure they were written for.
            # `pymodbus.exceptions.ModbusIOException` -- what a TIMEOUT raises, by far the
            # commonest way a Modbus read fails -- does NOT subclass OSError:
            #
            #   ModbusIOException -> ModbusException -> Exception
            #
            # so it sailed past every one of them into `run()`'s catch-all, killing the whole
            # tick and leaving a hole in dispatch_state. Observed twice on 2026-08-18 (13:03
            # and 16:00), each time two consecutive ticks; the inverter answered normally on
            # either side. Converting here, at the one boundary where pymodbus's exception
            # vocabulary enters this file, is what makes those handlers real -- the
            # alternative, four separate `except (OSError, ModbusException)` clauses, is four
            # places for the next reader to miss one.
            raise OSError(f"read {addr} failed: {e}") from e
        if r.isError():
            raise OSError(f"read {addr} failed: {r}")
        return R.decode(r.registers, signed)

    async def read_raw_block(self) -> list[int]:
        """The nine dispatch words, verbatim.

        Kept separate from `read_block` because section 7.1 publishes BOTH the decoded fields
        and the raw block: the decoded ones are for reading, the raw ones are for the morning
        after, when a decode turns out to have been wrong.
        """
        addr, count = R.DISPATCH_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"dispatch block read failed: {e}") from e
        if r.isError():
            raise OSError(f"dispatch block read failed: {r}")
        return list(r.registers)

    async def read_temp_block(self) -> list[int]:
        """The six battery cell-temperature words, verbatim.

        One more read in a sequence that already does several per tick, over the same
        connection -- not a new category of operation. `registers.decode_temp_block` turns it
        into fields; this side only moves words.
        """
        addr, count = R.TEMP_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"temp block read failed: {e}") from e
        if r.isError():
            raise OSError(f"temp block read failed: {r}")
        return list(r.registers)

    async def read_fault_block(self) -> list[int]:
        """The 22 fault/warning words, verbatim. See `registers.FAULT_BLOCK`."""
        addr, count = R.FAULT_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"fault block read failed: {e}") from e
        if r.isError():
            raise OSError(f"fault block read failed: {r}")
        return list(r.registers)

    async def read_firmware_block(self) -> list[int]:
        """The 6 firmware/battery-identity words, verbatim. See `registers.FIRMWARE_BLOCK`."""
        addr, count = R.FIRMWARE_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"firmware block read failed: {e}") from e
        if r.isError():
            raise OSError(f"firmware block read failed: {r}")
        return list(r.registers)

    async def read_inverter_fw_block(self) -> list[int]:
        """The 20 inverter firmware/serial words, verbatim. See `registers.INVERTER_FW_BLOCK`."""
        addr, count = R.INVERTER_FW_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"inverter firmware block read failed: {e}") from e
        if r.isError():
            raise OSError(f"inverter firmware block read failed: {r}")
        return list(r.registers)

    async def read_system_config_block(self) -> list[int]:
        """The 16 system-config words, verbatim. See `registers.SYSTEM_CONFIG_BLOCK`."""
        addr, count = R.SYSTEM_CONFIG_BLOCK
        try:
            r = await self.c.read_holding_registers(addr,
                                                    **{"count": count, self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"system config block read failed: {e}") from e
        if r.isError():
            raise OSError(f"system config block read failed: {r}")
        return list(r.registers)

    async def read_block(self) -> dict:
        return R.decode_block(await self.read_raw_block())

    async def write(self, addr: int, values: list[int]):
        if self.dry_run:
            log.info("      [dry-run] addr=%s values=%s", addr, values)
            return
        try:
            r = await self.c.write_registers(addr, values, **{self.kw: self.slave})
        except ModbusException as e:  # see Inverter.read
            raise OSError(f"write {addr}={values} failed: {e}") from e
        if r.isError():
            raise OSError(f"write {addr}={values} failed: {r}")

    async def apply(self, cmd: R.Command):
        """Payload first, START last.

        The ordering is the safety property: until START is set, a partially written command
        is inert. Writing START first would briefly run the PREVIOUS command's power and mode
        against the new duration. Ported from the handover dispatcher, which does the same.
        """
        for addr, words in R.encode_command(cmd).items():
            await self.write(addr, words)
        await self.write(R.REG_START, [1])

    async def release(self):
        await self.write(R.REG_START, [0])

    async def limits(self) -> tuple[int | None, int | None]:
        """Charge and discharge ceilings, each read (and degraded) independently.

        Two separate registers, each in its own try -- a shared one originally wrapped both
        reads together, which meant a discharge-register timeout threw away a charge limit
        that had just been read successfully. `slots.clamp()` already treats a `None` half
        independently of the other (a derate on one side is not a derate on both), so there
        was no reason for this function to be less granular than its own caller.
        """
        try:
            max_charge = await self.read(R.REG_MAX_CHARGE_POWER)
        except OSError as e:
            # Not fatal. Losing the clamp is worse than not having it only if the plan is
            # asking for something out of range, which the translator should already prevent.
            log.warning("could not read inverter charge limit (%s) -- proceeding unclamped "
                        "for charging", e)
            max_charge = None
        try:
            max_discharge = await self.read(R.REG_MAX_DISCHARGE_POWER)
        except OSError as e:
            log.warning("could not read inverter discharge limit (%s) -- proceeding "
                        "unclamped for discharging", e)
            max_discharge = None
        return max_charge, max_discharge


def configure_logging(retention_days: int, verbose: bool):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.TimedRotatingFileHandler(
        "dispatch_audit.log", when="D", interval=1, backupCount=retention_days)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def write_heartbeat(decision: S.Decision, state: dict | None, live_soc: float | None,
                    surplus_w: float | None = None):
    """Written every tick, dry-run or live.

    This answers the one question no register can: is the process alive and looking at the
    clock? It matters more here than in most loops, because section 4.1 makes `start=0` a
    legitimate commanded state -- so at the register level "deliberately running
    self-consumption" and "crashed an hour ago" are identical. Only this file tells them
    apart.
    """
    payload = {
        "checked_in_at": dt.datetime.now(dt.UTC).isoformat(),
        "kind": decision.kind,
        "reason": decision.reason,
        "fresh": decision.fresh,
        "slot": decision.slot,
        "live_soc_pct": live_soc,
        "surplus_w": surplus_w,
        "readback": state,
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(HEARTBEAT_PATH)


def monitor_pings(decision: S.Decision, cache: dict, live_soc: float | None,
                  dry_run: bool) -> list[tuple[str, str, str]]:
    """(monitor, status, message) for section 6.1's #4-#8. Pure -- the I/O is the caller's.

    All five are answered from one tick's worth of facts, so they are decided in one place;
    scattering five `send_heartbeat` calls through `tick()` is how a monitor ends up silently
    never pinged, which is the state this function exists to end.

    Three of the five have a deliberate NON-obvious rule:

      #6 `dispatch-confirmed` is UP when there was nothing to confirm. A release or an idle
         tick writes no command, so a readback proving nothing is not evidence of a rejected
         write -- and this monitor is "the inverter is rejecting writes", not "a command is
         live". In dry run it is up unconditionally: nothing is written, so nothing can land.

      #8 `soc-floor` is not pinged at all when the SoC register could not be read. A `down`
         there would say "the battery is below its floor", which is not what was observed.
         Silence is the honest answer, and the 15-minute window turns a persistent read
         failure into a `down` on its own without turning a single dropped read into one.

      #4 `slots-fresh` carries `decision.reason` verbatim on the way down, because the two
         ways to be stale have different fixes -- a dead translator vs a plan that ran out --
         and that string is the only thing that distinguishes them on a phone.
    """
    pings = [("dispatcher-alive", "up", f"{decision.kind}: {decision.reason}")]

    if decision.fresh:
        pings.append(("slots-fresh", "up", f"{decision.kind}: {decision.reason}"))
    else:
        pings.append(("slots-fresh", "down", decision.reason))

    verified = cache.get("write_verified")
    if dry_run:
        pings.append(("dispatch-confirmed", "up", "dry run -- nothing written"))
    elif verified is False:
        pings.append(("dispatch-confirmed", "down", "write did not verify: the block does "
                                                    "not hold what was just written"))
    else:
        pings.append(("dispatch-confirmed", "up",
                      "verified" if verified else "nothing commanded this tick"))

    if cache.get("hijacked"):
        block = cache.get("hijack_state") or {}
        pings.append(("inverter-not-hijacked", "down",
                      f"block active with mode={block.get('mode')} "
                      f"power={block.get('power_w')}W soc={block.get('target_soc_pct')}% "
                      f"that this process did not write"))
    else:
        pings.append(("inverter-not-hijacked", "up", "OK"))

    if live_soc is not None:
        status = "up" if live_soc >= S.SOC_FLOOR_PCT else "down"
        pings.append(("soc-floor", status, f"SoC {live_soc:.1f}% (floor {S.SOC_FLOOR_PCT}%)"))

    return [(name, status, msg[:200]) for name, status, msg in pings]


async def report(pings: list[tuple[str, str, str]]) -> None:
    """Send the pings without blocking the control loop.

    `send_heartbeat` is `urllib`, which is synchronous, and five of them at a 5 s timeout is up
    to 25 s -- inside a 60 s loop that also has to talk to an inverter. Off the event loop and
    concurrently, so a Kuma outage costs the loop nothing at all.
    """
    await asyncio.gather(*(
        asyncio.to_thread(send_heartbeat, MONITOR_URLS.get(name, ""), status, msg)
        for name, status, msg in pings))


def check_alive() -> int:
    """Pure local file check -- deliberately no Modbus.

    Opening a connection to ask "are you alive" would steal the inverter's single slot from
    the very process being checked. Same reason section 6.2 forbids a Kuma TCP port monitor
    on :502.
    """
    if not HEARTBEAT_PATH.exists():
        print(f"No heartbeat at {HEARTBEAT_PATH.resolve()} -- scheduler has never run here.")
        return 2
    p = json.loads(HEARTBEAT_PATH.read_text())
    age = (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(p["checked_in_at"])).total_seconds()
    stale = age > S.REFRESH_INTERVAL_S * 2
    print(f"[{'STALE' if stale else 'ALIVE'}] last check-in {age:.0f}s ago")
    print(f"  {p['kind']}: {p['reason']}")
    if p.get("live_soc_pct") is not None:
        print(f"  live SoC {p['live_soc_pct']}%")
    return 1 if stale else 0


async def _read_weekly_block(cache: dict, now: dt.datetime, name: str, read_words, decode
                             ) -> dict | None:
    """One weekly-tier block's independent gate, read, and backoff. Used three times from
    step 8d, once each for `registers.FIRMWARE_BLOCK`/`INVERTER_FW_BLOCK`/`SYSTEM_CONFIG_BLOCK`
    -- `name` keys this block's own `cache` entries, `read_words` is the bound `Inverter`
    method that fetches its raw words, `decode` is the matching `registers.decode_*_block`.

    THREE INDEPENDENT GATES, not one shared one. These are three separate register ranges with
    independent support and independent failure: a firmware block this inverter does not
    support must not also silence, or slow the retry of, the inverter-firmware or
    system-config blocks, which may read back fine on their own schedule. An earlier version
    shared one `weekly_health_read_at`/`weekly_health_ok` pair across all three, which had two
    bugs at once -- a lone failing block was masked by its two working siblings (the gate
    still advanced a full week, `any()` rather than `all()`), and once the aggregate did flip
    unhealthy, ALL THREE retried hourly together even though only one was actually failing.

    BACKS OFF TO HEALTH_REFRESH_S AFTER A FAILURE, THEN GIVES UP AFTER
    WEEKLY_FAIL_STREAK_LIMIT CONSECUTIVE ONES. A transient timeout should not cost a full week
    before the next attempt -- that was the original bug this replaces -- but a register that
    has failed WEEKLY_FAIL_STREAK_LIMIT hours running is not transient, it is a register this
    hardware does not have, and retrying it hourly forever is the same Modbus-budget risk the
    `read_error` skip in 8c/8d exists to bound. Past the limit the interval reverts to the
    full week: still checked, just rarely, the same posture the block had before it started
    failing.
    """
    read_at_key, streak_key, error_key = (
        f"weekly_{name}_read_at", f"weekly_{name}_fail_streak", f"weekly_{name}_error")
    read_at = cache.get(read_at_key)
    streak = cache.get(streak_key, 0)
    interval = (HEALTH_REFRESH_S if 0 < streak < WEEKLY_FAIL_STREAK_LIMIT
               else WEEKLY_HEALTH_REFRESH_S)
    if read_at is not None and (now - read_at).total_seconds() < interval:
        return None

    error, decoded = "", None
    try:
        decoded = decode(await read_words())
    except (OSError, ValueError) as e:
        error = f"{name.replace('_', ' ')} block read failed: {e}"

    # Warn once per NEW failure, debug on repeats, announce a recovery -- same shape as every
    # other gated read in this file, kept independent per block so a second block failing
    # while a first is already known-failing still gets its own WARNING rather than hiding
    # behind the first one's already-quieted debug logging.
    was_failing = cache.get(error_key, "")
    if error and not was_failing:
        log.warning("%s -- publishing no field for this block (further failures at debug)",
                    error)
    elif error:
        log.debug("%s", error)
    elif was_failing:
        log.info("%s block readings recovered", name.replace("_", " "))
    cache[error_key] = error
    cache[read_at_key] = now
    cache[streak_key] = 0 if decoded is not None else streak + 1
    return decoded


async def tick(inv: Inverter, slots_path: Path, cache: dict, now: dt.datetime) -> S.Decision:
    """One pass of section 5. Returns the decision, for the caller to report on."""
    # 1. Reload slots.json only when it changed on disk.
    try:
        # stat() failing is not handled here on purpose. Letting it fall through to load()
        # means the operator gets "slots.json does not exist -- has the translator ever run?"
        # instead of a bare errno: the file is missing in both cases, but only one of those
        # answers the question actually being asked at 03:00.
        try:
            mtime = slots_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is None or cache.get("mtime") != mtime:
            cache["doc"] = S.load(slots_path)
            cache["mtime"] = mtime
            cache["error"] = ""
            log.info("loaded %s: %d slots, horizon_end=%s",
                     slots_path, len(cache["doc"]["slots"]), cache["doc"]["horizon_end"])
    except (OSError, S.SlotsError) as e:
        cache["doc"], cache["error"] = None, str(e)

    # 4. Live SoC, read before deciding -- the direction rule needs it.
    #
    # `read_error` accumulates why the inverter could not be read, so that a tick which
    # decided but could not see the hardware still publishes SOMETHING. See step 9.
    read_error = ""
    try:
        live_soc = await inv.read(R.REG_BATTERY_SOC) / 10
    except OSError as e:
        log.warning("SoC read failed: %s", e)
        live_soc, read_error = None, str(e)

    # Surplus generation, for the one decision that needs to know whether the sun is beating
    # the house: a charge whose target is already met freezes the battery, and freezing while
    # PV is spilling exports free solar (`slots._charge_target_reached`).
    #
    # Two registers rather than the grid meter alone, because grid power moves when we act and
    # generation-minus-load does not -- the identity and the measurements are in
    # `slots.SURPLUS_HARVEST_W`. A failed read is None, not zero: None falls back to the old
    # freeze, zero would claim the house is eating everything it makes.
    try:
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
    except OSError as e:
        log.warning("surplus read failed: %s -- a met charge target will hold, not release", e)
        surplus_w, batt_w = None, None

    # Charging-positive, matching `setpoint_w` -- REG_BATTERY_POWER is discharge-positive.
    # `None`, not 0, when the read failed or looked implausible: 0 would publish "the battery
    # is idle" for "unknown", which is exactly the lie `soc_pct` was made conditional to avoid.
    actual_battery_w = -batt_w if batt_w is not None else None

    # Shortfall against the PREVIOUS tick's command. `batt_w` just read reflects up to TICK_S
    # seconds of settling under `cache["last_written"]` -- the command THIS tick is about to
    # replace, not the one it is about to issue.
    #
    # GATED ON LIVE. In dry run `cache["last_written"]` is still set to the command a decision
    # chose, even though `Inverter.write` never sent it -- so comparing it to real battery
    # power would score ordinary self-consumption against a command nobody issued, exactly the
    # trap `review-dry-run.py`'s docstring warns about ("because nothing is commanded, the
    # battery ... will disagree with the decisions constantly").
    #
    # ONE LOG LINE PER TRANSITION, not one per tick. MEASURED 2026-08-24: a sustained discharge
    # session held ~5-7% under its setpoint for 149 minutes straight -- at 60 s a tick that is
    # 149 near-identical WARNINGs, the same problem `StatePublisher._failing` exists to avoid.
    shorted = False
    if not inv.dry_run and actual_battery_w is not None:
        prev_cmd = cache.get("last_written")
        if prev_cmd is not None and prev_cmd.mode == R.DispatchMode.SOC_TARGET \
                and prev_cmd.power_w != 0:
            shortfall_w = abs(prev_cmd.power_w) - abs(actual_battery_w)
            shorted = (shortfall_w >= S.SHORTFALL_MIN_W
                       and shortfall_w / abs(prev_cmd.power_w) >= S.SHORTFALL_PCT)

    was_shorted = cache.get("shorted", False)
    if shorted and not was_shorted:
        log.warning(
            "magnitude shortfall: commanded %+dW, battery delivering %+.0fW (%.0f%% short) -- "
            "registers verified, so this is the inverter under-delivering, not an unlanded "
            "write", prev_cmd.power_w, actual_battery_w,
            100 * shortfall_w / abs(prev_cmd.power_w))
    elif was_shorted and not shorted:
        log.info("magnitude shortfall cleared")
    cache["shorted"] = shorted

    decision = S.decide(cache.get("doc"), now, live_soc, cache.get("error", ""), surplus_w)

    # 5. Hijack check, before we overwrite the evidence.
    #
    # `write_verified` still holds the PREVIOUS tick's value here -- step 8 below overwrites
    # it for this tick. False means that tick's own write never confirmed landing, so a
    # mismatch below is not necessarily someone else's doing.
    prev_write_confirmed = cache.get("write_verified") is not False
    try:
        raw_before = await inv.read_raw_block()
        state = R.decode_block(raw_before)
        cache["raw_words"] = raw_before
    except OSError as e:
        log.warning("dispatch block read failed: %s", e)
        state, read_error = None, str(e)

    if state is not None and S.is_hijacked(state, cache.get("last_written"),
                                            last_write_confirmed=prev_write_confirmed):
        log.warning("HIJACK: block active with mode=%s power=%+dW soc=%.1f%% that we did not "
                    "write -- the app is dispatching", state["mode"], state["power_w"],
                    state["target_soc_pct"])
        cache["hijacked"] = True
        cache["hijack_state"] = state
    else:
        cache["hijacked"] = False
        cache["hijack_state"] = None

    # 6-7. Act. `wrote` records whether this tick touched a register at all, which decides
    # below whether the published state needs a fresh readback.
    wrote = False
    if decision.kind == "command":
        cmd, warn = S.clamp(decision.command, *cache.get("limits", (None, None)))
        if warn:
            log.warning("%s", warn)
        await inv.apply(cmd)
        cache["last_written"] = cmd
        cache["released"] = False
        wrote = True
    elif decision.kind == "release":
        await inv.release()
        cache["last_written"] = None
        cache["released"] = True
        wrote = True
    else:
        # "idle" -- silence is the fail-safe. But release ONCE on the way in rather than
        # leaving a stale command running for its full 300 s: a plan that just went stale
        # should not keep discharging at 4.5 kW for another five minutes. After that first
        # release, write nothing at all, which is what section 1 means by silence.
        if not cache.get("released"):
            log.info("entering idle (%s) -- releasing once, then silent", decision.reason)
            await inv.release()
            cache["last_written"] = None
            cache["released"] = True
            wrote = True

    # 8. Verify, then publish. A SECOND read of the same block, after acting.
    #
    # The read at step 5 reflects the PREVIOUS command -- it has to, it is taken before we
    # write. Publishing that would lag the dashboard by a tick and, worse, would show a
    # command as live before it had actually landed. So the state that reaches Influx is
    # always a post-write readback.
    #
    # Keyed on `wrote` rather than on the decision kind, because the idle path writes too: it
    # releases ONCE on the way in. Skipping the readback there published a block still showing
    # `dispatch_active=1` from before that release, so the dashboard said a command was live
    # on the very tick the loop gave up -- the one tick where that reading is most misleading.
    verified, raw_words = state, cache.get("raw_words")
    if not inv.dry_run and wrote:
        try:
            raw_words = await inv.read_raw_block()
            verified = R.decode_block(raw_words)
        except OSError as e:
            log.warning("verify read failed: %s", e)
            verified, raw_words, read_error = None, None, str(e)

    if verified is not None and decision.kind == "command":
        cmd = cache.get("last_written")
        ok = bool(verified["dispatch_active"]) and cmd is not None and S.matches_command(
            verified, cmd)

        if not ok and not inv.dry_run:
            # ONE RE-READ BEFORE CRYING WOLF. The block does not update atomically: START is
            # written last and lands first, so a readback taken microseconds later can show
            # `dispatch_active=1` beside a `mode` and `power` the device has not ingested yet.
            #
            # OBSERVED 2026-08-20 15:16:29Z, the first tick after a rebuild: wrote
            # mode=2 +4848 W to 90.5 %, read back `mode=0 power=0 duration=90` with
            # dispatch_active already 1 -- the half-applied state of a block that was released
            # a second earlier by the outgoing container. The next tick read mode 2 at 4,848 W
            # and every tick since has verified.
            #
            # It matters because of what it costs when it is wrong: monitor #6 means "the
            # inverter is refusing our writes", and a DOWN on every single deploy is how a
            # monitor becomes something you scroll past. The retry is a READ, so it cannot
            # change what the battery is doing -- and if the second read still disagrees, the
            # alarm below fires exactly as it did before and now means what it says.
            await asyncio.sleep(VERIFY_RETRY_DELAY_S)
            try:
                raw_words = await inv.read_raw_block()
                verified = R.decode_block(raw_words)
                ok = bool(verified["dispatch_active"]) and S.matches_command(verified, cmd)
                if ok:
                    log.info("write verified on re-read after %.1f s -- the block was "
                             "mid-ingest on the first look", VERIFY_RETRY_DELAY_S)
            except OSError as e:
                log.warning("verify re-read failed: %s", e)

        if not ok and not inv.dry_run:
            # Monitor #6. A write that silently does not land is the failure this whole
            # design fears most: every log line says "commanded", the battery does nothing,
            # and no monitor notices.
            log.error("WRITE NOT VERIFIED: wrote mode=%s power=%+dW soc=%s, read back %s",
                      cmd.mode, cmd.power_w, cmd.target_soc_pct, verified)
        cache["write_verified"] = ok
    else:
        cache["write_verified"] = None

    # 8b. Battery cell temperature, min and max across the whole fleet of packs.
    #
    # Lettered rather than numbered because it is not a step in the control loop: the loop is
    # done deciding and done writing by the time this runs. It sits here, between the verify
    # and the publish, only because the point it rides on is written at step 9.
    #
    # AFTER THE WRITE, not with the measurement reads at step 4, and the ordering is the point.
    # Nothing below branches on this -- it is published and logged and that is all -- so it
    # has no business sitting in front of the one thing this loop exists to do. A Modbus
    # timeout costs the client's full retry ladder, about 12 s, and spent here that is 12 s
    # added to the delay before a command reaches the inverter; spent below, it delays a
    # dashboard field nobody is watching in real time.
    #
    # `ValueError` is caught beside `OSError` because `decode_temp_block` raises it on a short
    # reply -- a proxy or a firmware answering four words where six were asked for. That is a
    # bad read like any other and must degrade to no fields, not take the tick down with it:
    # an uncaught one here would reach `run()`'s catch-all and cost the whole `dispatch_state`
    # point for that minute, which is the 2026-08-18 failure this file's docstring is about.
    #
    # `temps_plausible` is the scale guard documented in `registers.TEMP_PLAUSIBLE_C`, and it
    # degrades exactly as the implausible power reading above does: publish nothing rather
    # than a number that is wrong by a factor, or a zero-filled block's freezing battery.
    temp_error = ""
    try:
        temps = R.decode_temp_block(await inv.read_temp_block())
        if not R.temps_plausible(temps):
            temp_error, temps = f"implausible cell temperatures {temps}", None
    except (OSError, ValueError) as e:
        temp_error, temps = f"temp block read failed: {e}", None

    # ONE WARNING PER TRANSITION, not one per tick, and this is the failure that most needs it:
    # a block this firmware does not support does not fail intermittently, it fails at 60 s
    # intervals forever -- 1,440 identical WARNINGs a day burying the lines that mean
    # something. Same trade `StatePublisher._failing` and the magnitude-shortfall line already
    # make. The repeats stay at debug rather than being dropped, so `--log-level DEBUG` still
    # answers "is it still failing right now".
    was_failing = cache.get("temp_error", "")
    if temp_error and not was_failing:
        log.warning("%s -- publishing no temperature (further failures at debug)", temp_error)
    elif temp_error:
        log.debug("%s", temp_error)
    elif was_failing:
        log.info("cell temperature readings recovered")
    cache["temp_error"] = temp_error

    # 8c. Hourly health tier: fault/warning words, and the inverter's own power limits
    # republished under the health-dashboard's field names.
    #
    # GATED, unlike 8b: these registers barely move within an hour, so reading them every tick
    # buys nothing and costs a second Modbus round-trip 59 ticks out of 60. Same
    # cache-timestamp-gate shape as `run()`'s LIMITS_REFRESH_S, but living in `tick()` itself,
    # which already takes `cache` and `now` -- that is what lets a test drive this gate
    # directly, the way `TestCellTemperature` already drives 8b, instead of needing to exercise
    # `run()`'s own while loop, which nothing in this codebase tests today.
    #
    # `cache.get("health_read_at")` being absent (a fresh process) counts as due, so a freshly
    # started dispatcher publishes these fields on its very first tick rather than leaving the
    # health dashboard empty for up to an hour after every deploy.
    #
    # The power limits are NOT read again here: `cache["limits"]` is already refreshed hourly by
    # `run()`'s own LIMITS_REFRESH_S gate, for the clamp in steps 6-7. A second hourly timer
    # re-reading the same registers would be two clocks answering one question. This gate only
    # republishes that already-fresh value under the health dashboard's own field names.
    #
    # SKIPPED WHEN `read_error` IS ALREADY SET. By this point in the tick, `read_error` means
    # at least one of SoC/dispatch-block/verify already failed to answer -- i.e. the inverter is
    # very likely unreachable right now. Adding a fault-block read on top of that pays a second
    # ~12 s timeout (the client's full retry ladder, same cost 8b's own comment documents) for a
    # read almost certain to fail too, delaying `write_heartbeat()`/the Kuma ping for no benefit.
    # Skipping leaves the gate due, so the very next tick tries again once the inverter recovers.
    health_read_at = cache.get("health_read_at")
    health_due = (health_read_at is None
                  or (now - health_read_at).total_seconds() >= HEALTH_REFRESH_S)
    faults, limits_hourly = None, None
    if health_due and not read_error:
        health_error = ""
        try:
            faults = R.decode_fault_block(await inv.read_fault_block())
        except (OSError, ValueError) as e:
            health_error, faults = f"fault block read failed: {e}", None
        limits_hourly = cache.get("limits")

        was_health_failing = cache.get("health_error", "")
        if health_error and not was_health_failing:
            log.warning("%s -- publishing no health fields (further failures at debug)",
                        health_error)
        elif health_error:
            log.debug("%s", health_error)
        elif was_health_failing:
            log.info("hourly health readings recovered")
        cache["health_error"] = health_error
        cache["health_read_at"] = now

    # 8d. Weekly health tier: firmware, inverter firmware/serial, and system config -- a
    # tripwire, not a trend, per the block comments in `registers.py`. Same `read_error` skip
    # as 8c -- three MORE block reads on top of 8c's one is the worst case that motivated the
    # skip in the first place (up to four extra ~12 s timeouts on a fresh process where every
    # weekly block and 8c's fault block are due at once against an unreachable inverter).
    #
    # Each block's own gate, backoff, and give-up-after-N-failures live in
    # `_read_weekly_block` -- see its docstring for why these three are independent rather
    # than sharing one timestamp.
    firmware, inverter_fw, system_config = None, None, None
    if not read_error:
        firmware = await _read_weekly_block(
            cache, now, "firmware", inv.read_firmware_block, R.decode_firmware_block)
        inverter_fw = await _read_weekly_block(
            cache, now, "inverter_fw", inv.read_inverter_fw_block, R.decode_inverter_fw_block)
        system_config = await _read_weekly_block(
            cache, now, "system_config", inv.read_system_config_block,
            R.decode_system_config_block)

    # 9. Publish. A tick that could not read the inverter STILL publishes.
    #
    # It used to publish nothing, which made an unreadable inverter and a dead dispatcher the
    # same shape in Influx: absence. That is the wrong default for the one series anybody
    # consults when something looks wrong -- `review-dry-run.py` reported both 2026-08-18
    # incidents as "no decision", which reads as "the loop stopped", and it took a container
    # inspect and a log dive to find out the loop had been fine all along.
    #
    # The degraded point deliberately carries the DECISION and not the registers. What the
    # dispatcher decided is known; what the inverter is doing is exactly what could not be
    # read, and inventing a value for it -- carrying the last one forward, or publishing a
    # zero -- would be a lie told by the process best placed to know better.
    cache["raw_words"] = raw_words
    publisher = cache.get("publisher")
    plan_run = (cache.get("doc") or {}).get("plan_run", "")
    # What the DISPATCHER knew this tick, as opposed to what the inverter said. Both point
    # shapes get it, because none of it depends on a register having been readable -- see
    # `state._decision_fields`. Until now these four went only to the log and to Kuma, so the
    # dashboard could show a command and never show that it failed to land.
    known = {
        "decision_kind": decision.kind,
        "reason": decision.reason,
        "live": not inv.dry_run,
        "live_soc_pct": live_soc,
        # NOT the local `verified`, which is the decoded block. This is step 8's verdict on
        # whether the write landed, and it is None whenever there was nothing to confirm.
        "write_verified": cache.get("write_verified"),
        # Read at step 4, from a register the dispatch block does not touch -- present even on
        # a tick that could not read the block at all, same argument as `live_soc_pct` above.
        "actual_battery_w": actual_battery_w,
        # Read at step 8b, from its own block, and `None` whenever that read failed or looked
        # implausible -- so an absent temperature field means "not read", never "cold".
        #
        # DELIBERATELY THE LAST READ OF THE TICK, unlike the two above: nothing decides on it,
        # so it belongs behind the write rather than in front of it. See step 8b.
        "temps": temps,
        # Read at steps 8c/8d, each `None` whenever its gate had not elapsed this tick or its
        # read failed -- an absent field means "not read this tick", never "nothing wrong".
        "faults": faults,
        "limits_hourly": limits_hourly,
        "firmware": firmware,
        "inverter_fw": inverter_fw,
        "system_config": system_config,
    }
    if publisher is not None:
        if verified is not None and raw_words is not None:
            publisher.publish(
                state_mod.build_fields(
                    verified, raw_words, now,
                    slot=decision.slot, plan_run=plan_run, **known),
                now=now)
        else:
            publisher.publish(
                state_mod.build_degraded_fields(
                    slot=decision.slot, plan_run=plan_run,
                    read_error=read_error or "no readback this tick", **known),
                now=now)

    write_heartbeat(decision, verified, live_soc, surplus_w)

    # 10. Report to Kuma. Last, so every ping describes a completed tick rather than one in
    # progress -- and after the heartbeat file, which is the check that must never depend on
    # the network.
    await report(monitor_pings(decision, cache, live_soc, inv.dry_run))

    log.info("%s | %s | soc=%s | temp=%s | verified=%s | %s", decision.kind, decision.reason,
             f"{live_soc:.1f}%" if live_soc is not None else "?",
             # Both extremes, not an average: the pair is the reading, and a spread between
             # them is itself the interesting thing. "?" for unread, as `soc` does.
             f"{temps['min_cell_temp_c']:.1f}/{temps['max_cell_temp_c']:.1f}C"
             if temps is not None else "?",
             cache.get("write_verified"),
             "HIJACKED" if cache.get("hijacked") else "ours")
    return decision


def build_publisher(url: str, token: str, org: str, bucket: str, sys_sn: str):
    """The `dispatch_state` publisher, or None when Influx is not configured.

    Optional on purpose. A laptop dry run should not need a token, and losing Influx must
    degrade the dashboard rather than the control loop -- section 7.1 is observability, and
    observability that can stop the battery working is worse than none.
    """
    if not (url and token):
        log.info("no INFLUX_URL/INFLUX_TOKEN -- dispatch_state will not be published")
        return None
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
    except ImportError:
        log.warning("influxdb-client not installed -- dispatch_state will not be published")
        return None

    client = InfluxDBClient(url=url, token=token, org=org)
    log.info("publishing %s to %s bucket %s", state_mod.MEASUREMENT, url, bucket)
    return state_mod.StatePublisher(
        client.write_api(write_options=SYNCHRONOUS), bucket, sys_sn)


async def run(ip, port, slave_id, slots_path, dry_run, once, interval, retention,
              publisher=None):
    client = AsyncModbusTcpClient(ip, port=port)
    await client.connect()
    if not client.connected:
        log.error("cannot connect to %s:%s -- is another Modbus client holding the "
                  "inverter's single connection?", ip, port)
        return 1

    inv = Inverter(client, slave_id, dry_run)
    cache: dict = {"released": False, "publisher": publisher}
    stop = asyncio.Event()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    except NotImplementedError:
        pass

    cache["limits"] = await inv.limits()
    cache["limits_read_at"] = dt.datetime.now(dt.UTC)
    log.info("inverter limits: charge=%s W discharge=%s W", *cache["limits"])

    try:
        while not stop.is_set():
            try:
                now = dt.datetime.now(dt.UTC)
                # Re-read the limits hourly. They are not constants -- see LIMITS_REFRESH_S.
                if (now - cache["limits_read_at"]).total_seconds() >= LIMITS_REFRESH_S:
                    fresh_limits = await inv.limits()
                    cache["limits_read_at"] = now
                    if fresh_limits != cache["limits"]:
                        log.info("inverter limits changed: charge=%s->%s W "
                                 "discharge=%s->%s W", cache["limits"][0], fresh_limits[0],
                                 cache["limits"][1], fresh_limits[1])
                    cache["limits"] = fresh_limits
                await tick(inv, Path(slots_path), cache, now)
            except Exception as e:
                # 9. Never exit on a tick failure. The next tick may succeed, and if it does
                # not, the dead man's switch reverts the inverter without our help.
                log.exception("tick failed, continuing: %s", e)
            if once:
                break
            # Sleep on the stop event rather than asyncio.sleep, so SIGTERM releases dispatch
            # immediately instead of after up to a full interval.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        try:
            await inv.release()
            log.info("dispatch released on exit")
        except Exception as e:
            log.error("COULD NOT RELEASE ON EXIT: %s -- a command may stay live for up to "
                      "%ss until its duration expires", e, S.DISPATCH_DURATION_S)
        client.close()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ip")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, default=0x55)
    p.add_argument("--slots", default="slots.json")
    p.add_argument("--live", action="store_true", help="Actually write (default: dry-run)")
    p.add_argument("--once", action="store_true", help="One tick, then exit")
    p.add_argument("--interval", type=int, default=S.REFRESH_INTERVAL_S)
    p.add_argument("--alive", action="store_true", help="Check the heartbeat file and exit")
    p.add_argument("--log-retention-days", type=int, default=10)
    p.add_argument("--influx-url", default=os.environ.get("INFLUX_URL", ""))
    p.add_argument("--influx-org", default=os.environ.get("INFLUX_ORG", "home"))
    p.add_argument("--influx-bucket", default=os.environ.get("INFLUX_BUCKET", "alphaess"))
    p.add_argument("--sys-sn", default=os.environ.get("ALPHAESS_SYS_SN", ""))
    p.add_argument("--no-publish", action="store_true",
                   help="Do not write dispatch_state to InfluxDB")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    if a.alive:
        sys.exit(check_alive())

    configure_logging(a.log_retention_days, a.verbose)
    if not a.ip:
        p.error("--ip is required unless --alive")
    if AsyncModbusTcpClient is None:
        sys.exit("pymodbus is not installed: pip install -r dispatch/requirements.txt")

    # The token is read from the environment only -- never a flag, so it cannot end up in a
    # shell history or a `ps` listing on a shared NAS.
    publisher = None if a.no_publish else build_publisher(
        a.influx_url, os.environ.get("INFLUX_TOKEN", ""), a.influx_org,
        a.influx_bucket, a.sys_sn)

    sys.exit(asyncio.run(run(a.ip, a.port, a.slave_id, a.slots, not a.live,
                             a.once, a.interval, a.log_retention_days, publisher)))


if __name__ == "__main__":
    main()
