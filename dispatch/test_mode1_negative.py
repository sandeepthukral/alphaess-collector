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

TWO PROBES, BECAUSE ONE QUESTION TURNED INTO TWO
  Default (`overcommand`) asks for MORE than the available surplus. That separates CAP
  from DEMAND, because only DEMAND has anywhere to get the difference: the grid.

  It was run live on 2026-08-16 and returned CAP -- 4786 W commanded into a ~2790 W
  surplus, median grid +8 W, no import on any sample. DEMAND is dead. But CAP is not
  thereby proved, because overcommand cannot see the difference between "Mode 1 capped
  the charge at surplus" and "Mode 1 was accepted and ignored, and ordinary
  self-consumption absorbed the surplus". Both predict charge == surplus. Every sample
  in that run fit both.

  `--undercommand` is the probe that separates THOSE, and it is the mirror image: ask
  for markedly LESS than the surplus and watch the EXPORT channel.

      setpoint honoured -> battery charges the commanded power, the rest exports
      setpoint ignored  -> battery charges the whole surplus, export stays near zero

  The signal is `surplus - commanded`, so a SMALL command is the informative one here --
  the exact opposite of the overcommand probe's "do not be timid" advice below. Both are
  right for their own probe.

  It was run live on 2026-08-16 and returned HONOURED, decisively. 402 W commanded into
  a ~1220 W surplus: the battery charged 331-361 W for the whole four-minute window while
  PV swung between 1657 and 2780 W. Surplus moved 1172 W across that window (sd 403 W) and
  the charge moved 30 W (sd 15 W), with export up ~1014 W. A battery merely self-consuming
  would have tracked the surplus; this one tracked the setpoint and ignored the surplus.

  So Mode 1 is a real, honoured, PV-only charge setpoint -- and NOT self-consumption: it
  exported over a kilowatt while the house was drawing 500 W. The `self` action of the
  dispatch design therefore still stays a RELEASE, contrary to the note in the CAP branch
  of the original design. What Mode 1 is good for is a charge that provably cannot import.

WHY THE COMMANDED POWER IS DERIVED, NOT PASSED
  Either probe is only decisive relative to the surplus that happens to be available, so
  the script measures surplus first and derives the setpoint from it -- above surplus for
  overcommand, a fraction of it for undercommand. If the light cannot support a decisive
  separation either way, the run is refused rather than reported.

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
    python3 dispatch/test_mode1_negative.py --ip 192.168.68.151 --live --undercommand

  Run at midday with low house load. The overcommand probe wants clear sky; the
  undercommand probe tolerates broken cloud, because it samples longer and reads
  medians -- see UNDER_JITTER.
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

# Addresses and encodings come from `registers.py` -- imported, never re-transcribed. An
# earlier copy of this script carried its own table, which is how 0x0883 (reactive power) once
# stood in for the mode register: two tables, one of them wrong, and nothing comparing them.
# The import works when the script is run by path because its own directory leads sys.path.
import registers as R  # noqa: E402  -- after the pymodbus guard, which must fail first

# THE SIGN CONVENTION HERE IS THE REGISTER'S, NOT `registers.Command`'s.
#
# This script speaks in raw dispatch terms -- `watts` below is negative for charging, matching
# 0x0881 -- because its whole subject is what the inverter does with a genuinely negative
# Pdispatch. `Command` flips that to charging-positive for the dashboards. Both conventions
# are correct in their own layer; the flip is deliberate and lives in `registers.encode_power`,
# which this script therefore does NOT use.

# Thresholds. All in watts unless noted.
MIN_SURPLUS = 200          # below this there is nothing for Mode 1 to charge from
MARGIN = 2000              # how far the command must exceed surplus to be decisive
MIN_MARGIN = 1500          # ...and the floor below which we refuse to run at all
MAX_POWER = 5000           # inverter ceiling. Same physical fact as slots.HARD_MAX_POWER_W;
                           # not imported, so this script stays runnable on its own.
IMPORT_CONFIRMED = 500     # grid import that cannot be read as measurement noise
MAX_SOC = 85               # above this there is no room to charge
SOLAR_JITTER = 300         # baseline solar spread that means a cloud is moving through

# Undercommand probe. The signal is `surplus - commanded` in the EXPORT channel, so these
# are tuned to keep that difference large rather than to keep the command large.
UNDER_FRACTION = 0.33      # ask for this share of the measured surplus
MIN_COMMAND = 300          # ...but never less: very small setpoints risk landing under some
                           # inverter minimum and being special-cased, which would look like
                           # "ignored" for an entirely different reason.
MIN_SEPARATION = 500       # surplus - commanded, below which export is inside the noise
MIN_SURPLUS_UNDER = 800    # the surplus that MIN_COMMAND + MIN_SEPARATION together imply
UNDER_JITTER = 800         # this probe tolerates more cloud than the overcommand one: it
                           # aims at a third of surplus, so a stale surplus estimate moves
                           # the setpoint slightly rather than collapsing the separation.
LOAD_DRIFT = 400           # baseline->during house-load move that makes the run weak: the
                           # expected export is computed from the BASELINE surplus, so a
                           # load stepping on mid-phase eats the very signal being read.


class Inverter:
    def __init__(self, client, slave_id, dry_run):
        self.c, self.slave, self.dry_run = client, slave_id, dry_run

    async def _read(self, addr, count=1, signed=False):
        r = await self.c.read_holding_registers(addr, **{"count": count, _ID_KWARG: self.slave})
        if r.isError():
            raise OSError(f"read {addr} failed: {r}")
        return R.decode(r.registers, signed)

    async def _write(self, addr, values):
        if self.dry_run:
            print(f"      [dry-run] would write addr={addr} values={values}")
            return
        await self.c.write_registers(addr, values, **{_ID_KWARG: self.slave})

    async def sample(self):
        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "soc_pct": await self._read(R.REG_BATTERY_SOC) / 10,
            "battery_w": await self._read(R.REG_BATTERY_POWER, signed=True),
            "grid_w": await self._read(R.REG_GRID_POWER, 2, signed=True),
            "solar_w": await self._read(R.REG_PV_METER, 2, signed=True),
            "d_start": await self._read(R.REG_START),
            "d_mode": await self._read(R.REG_MODE),
            # Register convention, not Command's -- see the note beside the import.
            "d_power_w": await self._read(R.REG_POWER, 2) - R.POWER_OFFSET,
            "d_soc_pct": round(await self._read(R.REG_SOC) * R.SOC_STEP, 1),
            "d_time_s": await self._read(R.REG_TIME, 2),
        }

    async def dispatch(self, mode, watts, target_soc, duration_s):
        await self._write(R.REG_MODE, [mode])
        await self._write(R.REG_POWER, R.encode_int32(R.POWER_OFFSET + watts))
        await self._write(R.REG_SOC, R.encode_soc(target_soc))
        await self._write(R.REG_TIME, R.encode_int32(duration_s))
        await self._write(R.REG_START, [1])

    async def release(self):
        await self._write(R.REG_START, [0])


def house_load(s):
    """solar + battery(discharge positive) + grid(import positive)."""
    return s["solar_w"] + s["battery_w"] + s["grid_w"]


def surplus_of(s):
    return s["solar_w"] - house_load(s)


def med(values):
    return sorted(values)[len(values) // 2]


def plan_command(surplus, undercommand):
    """Derive the commanded power from measured surplus. Returns (watts, why).

    `watts` is None when no decisive command exists in this light, and `why` is then the
    abort text. The sign is the REGISTER's: negative charges.
    """
    if undercommand:
        want = max(MIN_COMMAND, min(MAX_POWER, int(surplus * UNDER_FRACTION)))
        separation = surplus - want
        if separation < MIN_SEPARATION:
            return None, (
                f"surplus ~{surplus:+.0f}W leaves only {separation:.0f}W between it and the "
                f"smallest useful command ({want}W). Under {MIN_SEPARATION}W the expected "
                f"export is inside the noise a passing cloud makes, so 'honoured' and "
                f"'ignored' would not be distinguishable. Re-run in brighter light.")
        return -want, (
            f"decisive because {want}W is {separation:.0f}W BELOW the ~{surplus:+.0f}W "
            f"surplus: if the setpoint is honoured that {separation:.0f}W has nowhere to go "
            f"but the grid, so export jumps; if it is ignored the battery keeps taking the "
            f"whole surplus and export stays near zero.")

    want = min(MAX_POWER, int(surplus) + MARGIN)
    if want < surplus + MIN_MARGIN:
        return None, (
            f"surplus ~{surplus:+.0f}W is too close to the {MAX_POWER}W ceiling. The command "
            f"could only reach {want}W, which does not exceed surplus by the {MIN_MARGIN}W "
            f"needed to tell 'cap' from 'demand'. Re-run when surplus is lower, with a larger "
            f"house load running, or with --undercommand.")
    return -want, (
        f"decisive because {want}W exceeds the ~{surplus:+.0f}W surplus by "
        f"{want - surplus:.0f}W: if it caps, grid stays near zero; if it demands, grid "
        f"imports roughly that difference.")


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


def verdict(baseline, during, commanded_w, aborted, undercommand=False):
    """Read battery and grid TOGETHER. Grid alone is not enough: a kettle switching on
    mid-phase also produces import, and that must not be misread as force-charging."""
    if not during:
        return ["no samples during the command -- inconclusive."]

    base_surplus = sum(surplus_of(s) for s in baseline) / len(baseline)
    charge = [-s["battery_w"] for s in during]          # positive = charging
    grid = [s["grid_w"] for s in during]
    mid_charge = med(charge)
    mid_grid = med(grid)
    want = abs(commanded_w)

    lines = [f"baseline surplus ~{base_surplus:+.0f}W, commanded {commanded_w:+d}W, "
             f"observed charge ~{mid_charge:+.0f}W, grid ~{mid_grid:+.0f}W"]

    if undercommand:
        return lines + _undercommand_verdict(baseline, during, want, base_surplus,
                                             mid_charge, mid_grid)

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
        lines.append("   Re-run with --undercommand to separate those two. That probe asks for "
                     "less than surplus and reads the export channel, where they differ.")
    else:
        lines.append("=> FLAT. Mode 1 held at 0W despite genuine surplus and a negative power "
                     "command -- same behaviour as the 32000 run. Mode 1 is a freeze, not a "
                     "PV-charge mode. Useful as a nighttime hold, useless for surplus.")

    if base_surplus < MIN_SURPLUS:
        lines.append("!! surplus was marginal -- treat all of the above as weak.")
    return lines


def _undercommand_verdict(baseline, during, want, base_surplus, mid_charge, mid_grid):
    """Does the inverter honour a Mode 1 setpoint that is BELOW available surplus?

    Read as a delta, not an absolute: baseline export is rarely exactly zero, and only the
    CHANGE across the command boundary is attributable to the command. Two independent
    channels have to agree -- charge and export -- because either one alone can be moved by
    a cloud or a kettle.
    """
    expected = base_surplus - want              # export the setpoint would force, if honoured
    base_grid = med([s["grid_w"] for s in baseline])
    delta_export = base_grid - mid_grid         # grid is import-positive, so this is +export
    midpoint = (base_surplus + want) / 2        # charge lands either side of this

    lines = [f"expected export if honoured ~{expected:+.0f}W; observed export change "
             f"~{delta_export:+.0f}W (baseline grid ~{base_grid:+.0f}W)",
             f"charge ~{mid_charge:+.0f}W against commanded {want}W and surplus "
             f"~{base_surplus:+.0f}W (midpoint {midpoint:.0f}W)"]

    exported = delta_export > expected / 2
    charged_low = mid_charge < midpoint

    if exported and charged_low:
        lines.append("=> HONOURED. The battery took roughly what it was told to take and the "
                     "remaining surplus went to the grid. Mode 1 is a real setpoint, and CAP "
                     "is confirmed rather than merely consistent.")
    elif not exported and not charged_low:
        lines.append("=> IGNORED. The battery absorbed the whole surplus regardless of a "
                     f"{want}W setpoint, and export did not move. Mode 1 is accepted and "
                     "discarded; it is self-consumption under another name.")
        lines.append("   The `self` action should stay a dispatch RELEASE -- a command that "
                     "does nothing is strictly worse than none: same behaviour, one more "
                     "active-block state that reads as a hijack.")
    else:
        lines.append("=> INCONCLUSIVE. The two channels disagree -- export says "
                     f"{'honoured' if exported else 'ignored'} and charge says "
                     f"{'honoured' if charged_low else 'ignored'}. That is the signature of "
                     "something else moving during the window, not of a third behaviour. "
                     "Re-run in steadier conditions.")

    base_load = med([house_load(s) for s in baseline])
    during_load = med([house_load(s) for s in during])
    if abs(during_load - base_load) > LOAD_DRIFT:
        lines.append(f"!! house load moved {during_load - base_load:+.0f}W between baseline and "
                     f"command (>{LOAD_DRIFT}W). The expected export is derived from the "
                     "BASELINE surplus, so treat the above as weak and re-run.")
    if base_surplus < MIN_SURPLUS_UNDER:
        lines.append("!! surplus was marginal -- treat all of the above as weak.")
    return lines


async def run(ip, port, slave_id, dry_run, duration_s, settle_s, interval_s, out_csv,
              undercommand=False):
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
        probe = "undercommand" if undercommand else "overcommand"
        min_surplus = MIN_SURPLUS_UNDER if undercommand else MIN_SURPLUS
        jitter = UNDER_JITTER if undercommand else SOLAR_JITTER

        print(f"\n== pre-flight ({settle_s}s, {probe} probe) ==")
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
        if surplus < min_surplus:
            print(f"\nABORT: surplus ~{surplus:+.0f}W is below {min_surplus}W. With no PV "
                  "surplus, every hypothesis predicts the same battery. Re-run at midday with "
                  "low house load.")
            return 2
        if solar_spread > jitter:
            print(f"\nABORT: solar moved {solar_spread}W across the baseline (limit {jitter}W "
                  f"for the {probe} probe) -- a cloud is passing, so the surplus figure the "
                  "commanded power is derived from is already stale. Re-run in steadier light.")
            return 2

        watts, why = plan_command(surplus, undercommand)
        if watts is None:
            print(f"\nABORT: {why}")
            return 2

        target = min(100, soc + 10)
        print(f"\n== command: mode=1 power={watts}W target_soc={target}% duration={duration_s}s ==")
        print(f"   {why}")
        if dry_run:
            print("   [dry-run] nothing will be written. Re-run with --live.")

        await inv.dispatch(1, watts, target, duration_s)
        during, aborted = await watch(inv, duration_s, interval_s, rows, "during", stop,
                                      abort_on_import=True)

        print("\n== released; watching for the dead man's switch ==")
        await inv.release()
        after, _ = await watch(inv, settle_s, interval_s, rows, "after", stop)

        print("\n== verdict ==")
        for line in verdict(baseline, during, watts, aborted, undercommand):
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
    p.add_argument("--undercommand", action="store_true",
                   help="Ask for LESS than the available surplus and read the export channel. "
                        "Separates 'Mode 1 capped the charge' from 'Mode 1 was ignored', which "
                        "the default overcommand probe cannot.")
    p.add_argument("--duration", type=int, default=None,
                   help="Dead man's switch seconds (default 90 overcommand, 240 undercommand). "
                        "Overcommand costs grid energy only in the DEMAND case, which "
                        "early-aborts anyway; undercommand costs none, and runs longer because "
                        "its verdict is a median over a channel clouds also move.")
    p.add_argument("--settle", type=int, default=None,
                   help="Baseline/after window seconds (default 45, 60 undercommand)")
    p.add_argument("--interval", type=int, default=10, help="Seconds between samples")
    p.add_argument("--csv", default=None, help="Write all samples here")
    a = p.parse_args()
    duration = a.duration if a.duration is not None else (240 if a.undercommand else 90)
    settle = a.settle if a.settle is not None else (60 if a.undercommand else 45)
    sys.exit(asyncio.run(run(a.ip, a.port, a.slave_id, not a.live, duration,
                             settle, a.interval, a.csv, a.undercommand)))


if __name__ == "__main__":
    main()
