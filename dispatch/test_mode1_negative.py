"""Answer one question: what does Mode 1 do when Pdispatch is genuinely negative?

The official table qualifies Mode 1 ("battery charges from PV only, discharge
forbidden") with `Pdispatch < 32000`. The 2026-08-15 run sent exactly 32000 -- which
does not satisfy that -- and saw the battery sit flat at 0 W. So the documented case
has never actually been exercised. Two readings remain open:

  CAP     "charge from surplus, up to at most N watts"  -> Mode 1 is the right command
          for daytime surplus slots, and the `self` row of the dispatch design changes
          from "release dispatch" to a real command.
  DEMAND  "charge at N watts, pulling from the grid if PV cannot supply it"  -> Mode 1
          is Mode 2 with extra steps. Abandon it.

The discriminator is GRID POWER while the command is live, and the two readings differ
by kilowatts, not watts. One sample settles it.

WHY THE COMMANDED POWER IS DERIVED, NOT PASSED
  The test is only decisive when the commanded power EXCEEDS the available PV surplus.
  Command 1500 W into a 3000 W surplus and both readings predict the same thing: the
  battery charges at 1500 W and nothing is drawn from the grid. So the script measures
  surplus first and asks for more than that. If surplus is so large that even the 5 kW
  ceiling cannot exceed it by a clear margin, the run is refused rather than reported.

WHY IT ABORTS IF A DISPATCH IS ALREADY ACTIVE
  The AlphaESS app writes this same register block. On 2026-08-15 at 16:11 it was
  caught holding `mode=2 dpwr=-5000W dsoc=100.0% dt=5580s` -- a 93-minute force-charge
  from the grid, which is PRECISELY the signature this test looks for. If it asserts
  during the phase the result is a textbook false positive, so `start=1` at pre-flight
  is a hard stop, not a warning. Put the app in plain self-consumption mode and clear
  any price-based charge/discharge thresholds before running.

COST OF THE BAD OUTCOME
  5 kW for 90 s is about 125 Wh -- under six cents at any plausible price, and August
  midday day-ahead is often negative. Do not be tempted into a timid run: a short
  decisive test at full power is both cheaper and more informative than a long careful
  one that cannot separate the hypotheses.

SAFETY
  - Dry-run by default; --live is required to write anything.
  - Aborts unless there is real PV surplus, SoC has headroom, and nothing else is
    dispatching.
  - Releases dispatch on every exit path, including Ctrl+C and unhandled errors.
  - Early-aborts the moment grid import is confirmed -- the question is answered at
    that point and there is no reason to keep importing.
  - Holds the inverter's ONE Modbus connection, so the dispatcher/collector must not
    be running against it at the same time.

USAGE
    python3 dispatch/test_mode1_negative.py --ip 192.168.68.151          # dry run
    python3 dispatch/test_mode1_negative.py --ip 192.168.68.151 --live

  Run at midday, clear sky, low house load.
"""
import argparse
import asyncio
import csv
import inspect
import signal
import sys
from datetime import datetime

try:
    from pymodbus.client import AsyncModbusTcpClient
except ImportError:
    sys.exit("pymodbus is not installed. `pip install pymodbus` in whatever environment "
             "you run this from -- it is deliberately not a dependency of the collector, "
             "which never speaks Modbus.")

# pymodbus renamed the unit-id keyword between versions; both are still in the wild.
_PARAMS = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
_ID_KWARG = "device_id" if "device_id" in _PARAMS else "slave"

# --- dispatch block, 0x0880-0x0888 -------------------------------------------------
REG_START = 2176           # 0x0880  1 word
REG_POWER = 2177           # 0x0881  2 words, 32000 offset, >0 discharge / <0 charge
REG_MODE = 2181            # 0x0885  1 word   (0x0883-84 is reactive power, unused here)
REG_SOC = 2182             # 0x0886  1 word,  raw = pct / 0.4
REG_TIME = 2183            # 0x0887  2 words, seconds -- the dead man's switch

# --- measurement -------------------------------------------------------------------
REG_GRID_POWER = 33        # 0x0021  2 words, signed, W. Positive = importing.
REG_BATTERY_SOC = 258      # 0x0102  1 word,  raw / 10 -> %
REG_BATTERY_POWER = 294    # 0x0126  1 word,  signed, W. Positive = discharging.
REG_PV_METER = 161         # 0x00A1  2 words, signed, W. The AC-coupled PV meter --
                           # the DC MPPT registers read 0 forever on this site.

POWER_OFFSET = 32000

# Thresholds. All in watts unless noted.
MIN_SURPLUS = 200          # below this there is nothing for Mode 1 to charge from
MARGIN = 2000              # how far the command must exceed surplus to be decisive
MIN_MARGIN = 1500          # ...and the floor below which we refuse to run at all
MAX_POWER = 5000           # inverter ceiling
IMPORT_CONFIRMED = 500     # grid import that cannot be read as measurement noise
MAX_SOC = 85               # above this there is no room to charge
SOLAR_JITTER = 300         # baseline solar spread that means a cloud is moving through


def decode(regs, signed=False):
    if len(regs) == 1:
        value, bits = regs[0], 16
    else:
        value, bits = (regs[0] << 16) | regs[1], 32
    if signed and value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def encode_int32(value):
    value &= 0xFFFFFFFF
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


class Inverter:
    def __init__(self, client, slave_id, dry_run):
        self.c, self.slave, self.dry_run = client, slave_id, dry_run

    async def _read(self, addr, count=1, signed=False):
        r = await self.c.read_holding_registers(addr, **{"count": count, _ID_KWARG: self.slave})
        if r.isError():
            raise OSError(f"read {addr} failed: {r}")
        return decode(r.registers, signed)

    async def _write(self, addr, values):
        if self.dry_run:
            print(f"      [dry-run] would write addr={addr} values={values}")
            return
        await self.c.write_registers(addr, values, **{_ID_KWARG: self.slave})

    async def sample(self):
        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "soc_pct": await self._read(REG_BATTERY_SOC) / 10,
            "battery_w": await self._read(REG_BATTERY_POWER, signed=True),
            "grid_w": await self._read(REG_GRID_POWER, 2, signed=True),
            "solar_w": await self._read(REG_PV_METER, 2, signed=True),
            "d_start": await self._read(REG_START),
            "d_mode": await self._read(REG_MODE),
            "d_power_w": await self._read(REG_POWER, 2) - POWER_OFFSET,
            "d_soc_pct": round(await self._read(REG_SOC) * 0.4, 1),
            "d_time_s": await self._read(REG_TIME, 2),
        }

    async def dispatch(self, mode, watts, target_soc, duration_s):
        await self._write(REG_MODE, [mode])
        await self._write(REG_POWER, encode_int32(POWER_OFFSET + watts))
        await self._write(REG_SOC, [round(target_soc / 0.4)])
        await self._write(REG_TIME, encode_int32(duration_s))
        await self._write(REG_START, [1])

    async def release(self):
        await self._write(REG_START, [0])


def house_load(s):
    """solar + battery(discharge positive) + grid(import positive)."""
    return s["solar_w"] + s["battery_w"] + s["grid_w"]


def surplus_of(s):
    return s["solar_w"] - house_load(s)


def fmt(s):
    return (f"    {s['time']}  SoC {s['soc_pct']:5.1f}%  batt {s['battery_w']:+6d}W  "
            f"grid {s['grid_w']:+6d}W  solar {s['solar_w']:5d}W  "
            f"| start={s['d_start']} mode={s['d_mode']} "
            f"dpwr={s['d_power_w']:+6d}W dsoc={s['d_soc_pct']:5.1f}% dt={s['d_time_s']}s")


async def watch(inv, seconds, interval, rows, phase, stop, abort_on_import=False):
    """Sample repeatedly. Returns (samples, aborted_early).

    With abort_on_import, two consecutive confirmed imports end the window: at that
    point the question is answered and every further second is grid energy bought to
    learn nothing.
    """
    out, elapsed, consecutive = [], 0, 0
    while elapsed < seconds and not stop.is_set():
        s = await inv.sample()
        s["phase"] = phase
        out.append(s)
        rows.append(s)
        print(fmt(s))
        if abort_on_import:
            consecutive = consecutive + 1 if s["grid_w"] > IMPORT_CONFIRMED else 0
            if consecutive >= 2:
                print(f"\n  !! grid importing >{IMPORT_CONFIRMED}W on two consecutive samples "
                      f"-- question answered, releasing now rather than buying more.")
                return out, True
        await asyncio.sleep(min(interval, seconds - elapsed))
        elapsed += interval
    return out, False


def verdict(baseline, during, commanded_w, aborted):
    """Read battery and grid TOGETHER. Grid alone is not enough: a kettle switching on
    mid-phase also produces import, and that must not be misread as force-charging."""
    if not during:
        return ["no samples during the command -- inconclusive."]

    base_surplus = sum(surplus_of(s) for s in baseline) / len(baseline)
    charge = [-s["battery_w"] for s in during]          # positive = charging
    grid = [s["grid_w"] for s in during]
    mid_charge = sorted(charge)[len(charge) // 2]
    mid_grid = sorted(grid)[len(grid) // 2]
    want = abs(commanded_w)

    lines = [f"baseline surplus ~{base_surplus:+.0f}W, commanded {commanded_w:+d}W, "
             f"observed charge ~{mid_charge:+.0f}W, grid ~{mid_grid:+.0f}W"]

    if aborted or mid_grid > IMPORT_CONFIRMED:
        if mid_charge > base_surplus + MIN_MARGIN / 2:
            lines.append("=> DEMAND. Mode 1 charged well above the available surplus and pulled "
                         "the difference from the grid. It is Mode 2 with extra steps -- "
                         "ABANDON Mode 1 for surplus slots; keep releasing dispatch instead.")
        else:
            lines.append("=> IMPORTING, but the battery is NOT charging at the commanded power. "
                         "Something else moved -- most likely a house load stepped on. "
                         "Inconclusive; re-run when load is steadier.")
    elif mid_charge > MIN_SURPLUS:
        lines.append("=> CAP. Mode 1 charged from surplus and did NOT import despite being asked "
                     f"for {want}W. Safe as the daytime surplus command.")
        lines.append("   CAVEAT: this is indistinguishable from 'Mode 1 was ignored and plain "
                     "self-consumption carried on'. Same outcome either way for surplus slots, "
                     "but it does NOT prove the SoC target would be honoured -- see below.")
    else:
        lines.append("=> FLAT. Mode 1 held at 0W despite genuine surplus and a negative power "
                     "command -- same behaviour as the 32000 run. Mode 1 is a freeze, not a "
                     "PV-charge mode. Useful as a nighttime hold, useless for surplus.")

    if base_surplus < MIN_SURPLUS:
        lines.append("!! surplus was marginal -- treat all of the above as weak.")
    return lines


async def run(ip, port, slave_id, dry_run, duration_s, settle_s, interval_s, out_csv):
    client = AsyncModbusTcpClient(ip, port=port)
    await client.connect()
    if not client.connected:
        print("Could not connect. Is the dispatcher, the collector, or another Modbus "
              "client holding the inverter's single connection?")
        return 1

    inv = Inverter(client, slave_id, dry_run)
    rows = []
    stop = asyncio.Event()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    except NotImplementedError:
        pass

    rc = 0
    try:
        # --- pre-flight ------------------------------------------------------------
        print(f"\n== pre-flight ({settle_s}s) ==")
        baseline, _ = await watch(inv, settle_s, interval_s, rows, "baseline", stop)
        if not baseline:
            return 1
        last = baseline[-1]
        soc = last["soc_pct"]
        surplus = sum(surplus_of(s) for s in baseline) / len(baseline)
        solar_spread = max(s["solar_w"] for s in baseline) - min(s["solar_w"] for s in baseline)

        print(f"\n  SoC {soc}%   surplus ~{surplus:+.0f}W   solar spread {solar_spread}W")

        if last["d_start"] == 1:
            print("\nABORT: a dispatch is already active (start=1). Something else is driving "
                  "this register block -- almost certainly the AlphaESS app. Put it in plain "
                  "self-consumption mode, clear any price-based thresholds, and re-run. "
                  "Proceeding would risk recording the app's own force-charge as the result.")
            return 2
        if soc > MAX_SOC:
            print(f"\nABORT: SoC {soc}% is above {MAX_SOC}% -- not enough headroom to charge, "
                  "and the direction rule needs a target above the current SoC.")
            return 2
        if surplus < MIN_SURPLUS:
            print(f"\nABORT: surplus ~{surplus:+.0f}W is below {MIN_SURPLUS}W. With no PV "
                  "surplus, 'caps at surplus' and 'holds flat' look identical. Re-run at "
                  "midday with low house load.")
            return 2
        if solar_spread > SOLAR_JITTER:
            print(f"\nABORT: solar moved {solar_spread}W across the baseline -- a cloud is "
                  "passing, so the surplus figure the commanded power is derived from is "
                  "already stale. Re-run in steadier light.")
            return 2

        watts = -min(MAX_POWER, int(surplus) + MARGIN)
        if abs(watts) < surplus + MIN_MARGIN:
            print(f"\nABORT: surplus ~{surplus:+.0f}W is too close to the {MAX_POWER}W ceiling. "
                  f"The command could only reach {abs(watts)}W, which does not exceed surplus by "
                  f"the {MIN_MARGIN}W needed to tell 'cap' from 'demand'. Re-run when surplus is "
                  "lower, or with a larger house load running.")
            return 2

        target = min(100, soc + 10)
        print(f"\n== command: mode=1 power={watts}W target_soc={target}% duration={duration_s}s ==")
        print(f"   decisive because {abs(watts)}W exceeds the ~{surplus:+.0f}W surplus by "
              f"{abs(watts) - surplus:.0f}W: if it caps, grid stays near zero; if it demands, "
              f"grid imports roughly that difference.")
        if dry_run:
            print("   [dry-run] nothing will be written. Re-run with --live.")

        await inv.dispatch(1, watts, target, duration_s)
        during, aborted = await watch(inv, duration_s, interval_s, rows, "during", stop,
                                      abort_on_import=True)

        print("\n== released; watching for the dead man's switch ==")
        await inv.release()
        after, _ = await watch(inv, settle_s, interval_s, rows, "after", stop)

        print("\n== verdict ==")
        for line in verdict(baseline, during, watts, aborted):
            print("  " + line)

        if after and not any(s["d_start"] == 1 for s in after):
            print("  dead man's switch: released cleanly (also confirmed for Mode 1).")
        print(f"\n  NOT answered by this run: whether Mode 1 stops at target_soc. Reaching "
              f"{target}% from {soc}% is roughly 2.8 kWh, tens of minutes -- far past a "
              f"{duration_s}s window. That needs its own long run, and only matters if the "
              f"verdict above was CAP.")

    except Exception as e:
        print(f"\nERROR: {e}")
        rc = 1
    finally:
        try:
            await inv.release()
            print("\ndispatch released.")
        except Exception as e:
            print(f"\n!! COULD NOT RELEASE DISPATCH: {e}\n"
                  f"!! Check the inverter -- a command may still be live until its "
                  f"{duration_s}s duration expires.")
        client.close()
        if out_csv and rows:
            with open(out_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"wrote {out_csv} ({len(rows)} samples)")
    return rc


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ip", required=True)
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, default=0x55)
    p.add_argument("--live", action="store_true", help="Actually write (default: dry-run)")
    p.add_argument("--duration", type=int, default=90,
                   help="Dead man's switch seconds (default 90). Longer costs grid energy "
                        "only in the DEMAND case, which early-aborts anyway.")
    p.add_argument("--settle", type=int, default=45, help="Baseline/after window seconds")
    p.add_argument("--interval", type=int, default=10, help="Seconds between samples")
    p.add_argument("--csv", default=None, help="Write all samples here")
    a = p.parse_args()
    sys.exit(asyncio.run(run(a.ip, a.port, a.slave_id, not a.live, a.duration,
                             a.settle, a.interval, a.csv)))


if __name__ == "__main__":
    main()
