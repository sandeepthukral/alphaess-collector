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

try:
    from pymodbus.client import AsyncModbusTcpClient
except ImportError:  # --alive must work without pymodbus installed
    AsyncModbusTcpClient = None

log = logging.getLogger("dispatch")

HEARTBEAT_PATH = Path(os.environ.get("DISPATCH_HEARTBEAT", "dispatch_heartbeat.json"))


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
        r = await self.c.read_holding_registers(addr, **{"count": count, self.kw: self.slave})
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
        r = await self.c.read_holding_registers(addr, **{"count": count, self.kw: self.slave})
        if r.isError():
            raise OSError(f"dispatch block read failed: {r}")
        return list(r.registers)

    async def read_block(self) -> dict:
        return R.decode_block(await self.read_raw_block())

    async def write(self, addr: int, values: list[int]):
        if self.dry_run:
            log.info("      [dry-run] addr=%s values=%s", addr, values)
            return
        r = await self.c.write_registers(addr, values, **{self.kw: self.slave})
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
        try:
            return (await self.read(R.REG_MAX_CHARGE_POWER),
                    await self.read(R.REG_MAX_DISCHARGE_POWER))
        except OSError as e:
            # Not fatal. Losing the clamp is worse than not having it only if the plan is
            # asking for something out of range, which the translator should already prevent.
            log.warning("could not read inverter limits (%s) -- proceeding unclamped", e)
            return None, None


def configure_logging(retention_days: int, verbose: bool):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.TimedRotatingFileHandler(
        "dispatch_audit.log", when="D", interval=1, backupCount=retention_days)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def write_heartbeat(decision: S.Decision, state: dict | None, live_soc: float | None):
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
        "readback": state,
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(HEARTBEAT_PATH)


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
    try:
        live_soc = await inv.read(R.REG_BATTERY_SOC) / 10
    except OSError as e:
        log.warning("SoC read failed: %s", e)
        live_soc = None

    decision = S.decide(cache.get("doc"), now, live_soc, cache.get("error", ""))

    # 5. Hijack check, before we overwrite the evidence.
    try:
        raw_before = await inv.read_raw_block()
        state = R.decode_block(raw_before)
        cache["raw_words"] = raw_before
    except OSError as e:
        log.warning("dispatch block read failed: %s", e)
        state = None

    if state is not None and S.is_hijacked(state, cache.get("last_written")):
        log.warning("HIJACK: block active with mode=%s power=%+dW soc=%.1f%% that we did not "
                    "write -- the app is dispatching", state["mode"], state["power_w"],
                    state["target_soc_pct"])
        cache["hijacked"] = True
    else:
        cache["hijacked"] = False

    # 6-7. Act.
    if decision.kind == "command":
        cmd, warn = S.clamp(decision.command, *cache.get("limits", (None, None)))
        if warn:
            log.warning("%s", warn)
        await inv.apply(cmd)
        cache["last_written"] = cmd
        cache["released"] = False
    elif decision.kind == "release":
        await inv.release()
        cache["last_written"] = None
        cache["released"] = True
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

    # 8. Verify, then publish. A SECOND read of the same block, after acting.
    #
    # The read at step 5 reflects the PREVIOUS command -- it has to, it is taken before we
    # write. Publishing that would lag the dashboard by a tick and, worse, would show a
    # command as live before it had actually landed. So the state that reaches Influx is
    # always a post-write readback.
    verified, raw_words = state, cache.get("raw_words")
    if not inv.dry_run and decision.kind != "idle":
        try:
            raw_words = await inv.read_raw_block()
            verified = R.decode_block(raw_words)
        except OSError as e:
            log.warning("verify read failed: %s", e)
            verified, raw_words = None, None

    if verified is not None and decision.kind == "command":
        cmd = cache.get("last_written")
        ok = bool(verified["dispatch_active"]) and cmd is not None and S.matches_command(
            verified, cmd)
        if not ok and not inv.dry_run:
            # Monitor #6. A write that silently does not land is the failure this whole
            # design fears most: every log line says "commanded", the battery does nothing,
            # and no monitor notices.
            log.error("WRITE NOT VERIFIED: wrote mode=%s power=%+dW soc=%s, read back %s",
                      cmd.mode, cmd.power_w, cmd.target_soc_pct, verified)
        cache["write_verified"] = ok
    else:
        cache["write_verified"] = None

    cache["raw_words"] = raw_words
    publisher = cache.get("publisher")
    if publisher is not None and verified is not None and raw_words is not None:
        publisher.publish(
            state_mod.build_fields(
                verified, raw_words, now,
                decision_kind=decision.kind, slot=decision.slot,
                plan_run=(cache.get("doc") or {}).get("plan_run", "")),
            now=now)

    write_heartbeat(decision, verified, live_soc)
    log.info("%s | %s | soc=%s", decision.kind, decision.reason,
             f"{live_soc:.1f}%" if live_soc is not None else "?")
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
    log.info("inverter limits: charge=%s W discharge=%s W", *cache["limits"])

    try:
        while not stop.is_set():
            try:
                await tick(inv, Path(slots_path), cache, dt.datetime.now(dt.UTC))
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
