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

  The signal is `baseline charge - commanded`, so a SMALLER command is the informative one
  here -- the opposite of the overcommand probe's "do not be timid" advice below. Both are
  right for their own probe. But not arbitrarily small: the command has to stay far enough
  ABOVE zero that "charged the setpoint" and "charged nothing" are still distinguishable, so
  it is set at HALF the baseline charge rather than as little as possible. UNDER_FRACTION
  says why that number and not a smaller one.

  It was run live on 2026-08-16 and returned HONOURED, decisively. 402 W commanded into
  a ~1220 W surplus: the battery charged 331-361 W for the whole four-minute window while
  PV swung between 1657 and 2780 W. Surplus moved 1172 W across that window (sd 403 W) and
  the charge moved 30 W (sd 15 W), with export up ~1014 W. A battery merely self-consuming
  would have tracked the surplus; this one tracked the setpoint and ignored the surplus.

  That verdict was RE-CHECKED against the corrected logic below, because the version that
  produced it would have printed HONOURED for a battery sitting flat at 0 W (it tested only
  that the charge was BELOW the midpoint, never that it was NEAR the setpoint). The recorded
  numbers pass the two-sided test on their own merits -- |346 - 402| = 56 W, well inside
  tolerance, and baseline grid was -1 W so the surplus/charge distinction below did not bite
  -- so the conclusion stands. It was luck that it did.

  THE UNDERCOMMAND PROBE MEASURES AGAINST THE BASELINE CHARGE, NOT THE SURPLUS.
  Only power the battery was actually taking can be displaced into the export channel. A
  battery tapering at 600 W of a 1220 W surplus can free at most 600 W however bright it is;
  judging against the 1220 W would predict an export step physics cannot deliver, and burn a
  scarce clear-sky window on a false INCONCLUSIVE.

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
import time
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

# Undercommand probe. The signal is `baseline charge - commanded` in the EXPORT channel --
# baseline CHARGE, not surplus, because only power the battery was already taking can be
# displaced -- so these are tuned to keep that difference large rather than the command.
UNDER_FRACTION = 0.5       # ask for this share of the measured baseline charge. HALF, not a
                           # third, and the reason is the verdict rather than the command: the
                           # three hypotheses predict charges of 0, want and base_charge, and
                           # each gets a band one tolerance wide. At a third, `want/2` is
                           # smaller than the tolerance the OTHER gap needs, so the "flat" and
                           # "at the setpoint" bands are forced to abut and a battery clamping
                           # at 0.51x the setpoint reads as HONOURED. At a half the three
                           # predictions sit at 0, C/2 and C, evenly spaced, and one tolerance
                           # around each leaves equal dead zones on both sides. The cost is a
                           # smaller export signal -- half the baseline charge rather than two
                           # thirds -- which MIN_SEPARATION still guards.
MIN_COMMAND = 300          # ...but never less: very small setpoints risk landing under some
                           # inverter minimum and being special-cased, which would look like
                           # "ignored" for an entirely different reason.
MIN_SEPARATION = 500       # baseline charge - commanded, below which the expected export step
                           # is inside the noise a passing cloud makes. A NOISE floor only --
                           # keeping the charge bands disjoint is UNDER_FRACTION's job now.
CHARGE_TOL_FLOOR = 200     # how close to a prediction the observed charge must land, as a
                           # floor under `want/3`. The live 2026-08-16 run sat 56W below its
                           # setpoint with a standard deviation of 15W, so real regulation
                           # slop is tens of watts; 200W is generous without being a third of
                           # the setpoint itself.
MIN_CHARGE_UNDER = 1200    # the baseline charge below which the dead zones close up. At
                           # 1200W: command 600W, tolerance 200W, so the bands are 0-200,
                           # 400-800 and 1000-1400 -- 200W of INCONCLUSIVE on either side of
                           # the honoured band. Above it the gaps widen as want/3.
MIN_SURPLUS_UNDER = 800    # weaker second gate. Surplus >= charge whenever nothing is
                           # importing, so MIN_CHARGE_UNDER normally binds first; this still
                           # catches a baseline that is importing while the battery charges.
MIN_DURING_SAMPLES = 4     # every figure in the undercommand verdict is a median. A window
                           # cut short by `abort_on_import` can leave as few as two samples,
                           # and two samples do not make a median worth acting on.
UNDER_JITTER = 800         # this probe tolerates more cloud than the overcommand one: it
                           # aims at half the baseline charge, so a stale estimate
                           # moves the setpoint slightly rather than collapsing the
                           # separation. It does NOT widen the verdict's own error bars in
                           # the way it once did -- every channel below is a median, and the
                           # export test is a delta across the command boundary.
LOAD_DRIFT = 400           # baseline->during house-load move that makes the run weak: the
                           # expected export is computed from the BASELINE charge, so a
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


def plan_command(available, undercommand):
    """Derive the commanded power from the measured baseline. Returns (watts, why).

    `available` is what the command has to work against, and it is a DIFFERENT quantity for
    each probe -- which is the whole reason this takes one number rather than a sample list:

      overcommand   the PV SURPLUS. The question is whether the inverter will reach past it
                    to the grid, so the command has to exceed it.
      undercommand  the baseline CHARGE. The signal is the export the setpoint displaces, and
                    only power the battery was already taking can be displaced. Judging
                    against the surplus instead promises an export step the hardware cannot
                    produce whenever the battery is tapering below it.

    `watts` is None when no decisive command exists in this light, and `why` is then the
    abort text. The sign is the REGISTER's: negative charges.
    """
    if undercommand:
        want = max(MIN_COMMAND, min(MAX_POWER, int(available * UNDER_FRACTION)))
        separation = available - want
        if separation < MIN_SEPARATION:
            return None, (
                f"the battery is taking ~{available:+.0f}W, which leaves only "
                f"{separation:.0f}W between that and the smallest useful command ({want}W). "
                f"Under {MIN_SEPARATION}W the 'honoured' and 'ignored' charge predictions "
                f"overlap and the expected export is inside the noise a passing cloud makes. "
                f"Re-run in brighter light, or with a smaller house load.")
        return -want, (
            f"decisive because {want}W is {separation:.0f}W BELOW the ~{available:+.0f}W the "
            f"battery is already taking: if the setpoint is honoured that {separation:.0f}W "
            f"has nowhere to go but the grid, so export jumps; if it is ignored the battery "
            f"keeps taking what it was taking and export stays put.")

    want = min(MAX_POWER, int(available) + MARGIN)
    if want < available + MIN_MARGIN:
        return None, (
            f"surplus ~{available:+.0f}W is too close to the {MAX_POWER}W ceiling. The command "
            f"could only reach {want}W, which does not exceed surplus by the {MIN_MARGIN}W "
            f"needed to tell 'cap' from 'demand'. Re-run when surplus is lower, with a larger "
            f"house load running, or with --undercommand.")
    return -want, (
        f"decisive because {want}W exceeds the ~{available:+.0f}W surplus by "
        f"{want - available:.0f}W: if it caps, grid stays near zero; if it demands, grid "
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
    # A monotonic DEADLINE, not a counter incremented by `interval`. Each iteration also costs
    # eight sequential Modbus reads, which a counter does not see -- so the loop used to run
    # well past `seconds` of wall clock, and on the `during` phase could outlive the dead man's
    # switch it set and fold post-expiry samples into the medians.
    out, consecutive = [], 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
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
        await asyncio.sleep(max(0, min(interval, deadline - time.monotonic())))
    return out, False


def verdict_token(lines):
    """The one-word verdict. Callers -- and tests -- must not have to parse the prose."""
    for line in lines:
        if line.startswith("=> "):
            return line[3:].split(".", 1)[0].strip()
    return None


def command_carrying(during, commanded_w):
    """The samples whose register block still holds the command we wrote.

    The pre-flight refuses to start while somebody else is dispatching, but nothing stopped
    them starting afterwards -- and the AlphaESS app was caught doing exactly that at 16:11
    on 2026-08-15, mid-window. Those samples measure someone else's setpoint, so they cannot
    be averaged in silently.
    """
    return [s for s in during
            if s["d_start"] == 1 and s["d_mode"] == 1 and s["d_power_w"] == commanded_w]


def verdict(baseline, during, commanded_w, aborted, undercommand=False, dry_run=False):
    """Read battery and grid TOGETHER. Grid alone is not enough: a kettle switching on
    mid-phase also produces import, and that must not be misread as force-charging."""
    if not during:
        return ["=> INCONCLUSIVE. No samples during the command."]

    lines = []
    if not dry_run:
        carried = command_carrying(during, commanded_w)
        if len(carried) < len(during):
            lines.append(f"!! {len(during) - len(carried)} of {len(during)} samples did not "
                         f"carry the command that was written (start/mode/power changed "
                         f"underneath) -- the register block was driven by something else "
                         f"during the window. They are excluded from every median below.")
        during = carried
        if not during:
            lines.append("=> INCONCLUSIVE. Not one sample during the window carried the "
                         "command that was written, so nothing here measures it. Put the "
                         "AlphaESS app in plain self-consumption mode and re-run.")
            return lines

    # Both probes, because both read medians of a channel a cloud also moves. The exemption is
    # deliberate: an `abort_on_import` window ends on two consecutive CONFIRMED imports, and
    # for the overcommand probe that is the whole answer -- CAP and DEMAND differ by kilowatts,
    # so two samples settle it. Everything else here is a median and needs a median's worth.
    if not aborted and len(during) < MIN_DURING_SAMPLES:
        lines.append(f"=> INCONCLUSIVE. Only {len(during)} usable sample(s) during the "
                     f"command. Every figure below is a median and needs at least "
                     f"{MIN_DURING_SAMPLES}. Re-run.")
        return lines

    # Medians throughout -- `base_surplus` used to be the one mean left in the file, and it is
    # the most load-bearing channel there is: it sets the setpoint and every threshold below.
    base_surplus = med([surplus_of(s) for s in baseline])
    base_charge = med([-s["battery_w"] for s in baseline])      # positive = charging
    mid_charge = med([-s["battery_w"] for s in during])
    mid_grid = med([s["grid_w"] for s in during])
    want = abs(commanded_w)

    lines.append(f"baseline surplus ~{base_surplus:+.0f}W of which the battery took "
                 f"~{base_charge:+.0f}W, commanded {commanded_w:+d}W, observed charge "
                 f"~{mid_charge:+.0f}W, grid ~{mid_grid:+.0f}W")

    if undercommand:
        return lines + _undercommand_verdict(baseline, during, want, base_charge,
                                             mid_charge, mid_grid, aborted)

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


def _undercommand_verdict(baseline, during, want, base_charge, mid_charge, mid_grid, aborted):
    """Does the inverter honour a Mode 1 setpoint BELOW what the battery is already taking?

    THREE hypotheses, not two, and they predict three separated charge values:

        honoured  charge ~= want          export up by (base_charge - want)
        ignored   charge ~= base_charge    export flat
        flat      charge ~= 0              export up by the WHOLE base_charge

    With UNDER_FRACTION at a half those three sit evenly spaced at 0, C/2 and C, and each gets
    a band ONE TOLERANCE wide. Anything outside all three is INCONCLUSIVE, which is the honest
    answer for a battery that honours the sign but clamps the magnitude. FLAT is still decided
    first, because a frozen battery also pushes the export channel hard in the honoured
    direction -- the surplus has nowhere else to go -- and must not be rescued by it.

    Two earlier versions got this wrong in the same shape. The first asked only whether the
    charge was below the midpoint of (want, base_charge), which a frozen battery satisfies
    trivially: both channels "agreed" on HONOURED for a battery taking nothing at all. The
    second fixed that but cut FLAT at `want/2` while banding the other two at +/-tol, and with
    the command at a third of baseline that cut fell INSIDE the honoured band -- so HONOURED
    ran from 0.51x to 1.68x the setpoint with no gap at all on the low side. Hence one rule for
    all three predictions, and a fraction chosen so the rule has room to work.

    Everything is read as a DELTA, not an absolute: baseline export is rarely exactly zero,
    and only the change across the command boundary is attributable to the command.
    """
    if aborted:
        return ["=> INCONCLUSIVE. The window was cut short by confirmed grid import. This "
                "probe commands LESS than the battery was already taking, so there is no "
                "honest reading of the command under which it imports -- something else drove "
                "the register block, most likely the AlphaESS app. Put it in plain "
                "self-consumption mode, clear any price thresholds, and re-run."]
    expected = base_charge - want              # export the setpoint would force, if honoured
    base_grid = med([s["grid_w"] for s in baseline])
    delta_export = base_grid - mid_grid         # grid is import-positive, so this is +export
    exported = delta_export > expected / 2

    # ONE band, one tolerance wide, around each of the three predictions -- and nothing else
    # deciding any of them. An earlier version cut FLAT at `want/2` instead, a second heuristic
    # that had no relation to the tolerance the other two bands used: with the command at a
    # third of baseline it landed INSIDE the honoured band, so the honoured verdict ran from
    # 0.51x to 1.68x the setpoint with a hard edge against FLAT and a proper gap only on the
    # far side. A battery that honours the sign but clamps the magnitude read as a real
    # setpoint, which is precisely the claim this probe exists to certify.
    tol = max(CHARGE_TOL_FLOOR, want / 3)
    flat = abs(mid_charge) <= tol
    at_setpoint = abs(mid_charge - want) <= tol
    at_baseline = abs(mid_charge - base_charge) <= tol

    lines = [f"expected export if honoured ~{expected:+.0f}W; observed export change "
             f"~{delta_export:+.0f}W (baseline grid ~{base_grid:+.0f}W)",
             f"charge ~{mid_charge:+.0f}W against commanded {want}W and baseline charge "
             f"~{base_charge:+.0f}W (tolerance +/-{tol:.0f}W)"]

    if flat:
        lines.append("=> FLAT. The battery charged nothing at all, despite genuine surplus and "
                     f"a {want}W setpoint it was already exceeding. Export moved only because "
                     "the surplus had nowhere else to go, NOT because the setpoint was "
                     "honoured -- the same behaviour as the 32000 run. On this evidence Mode 1 "
                     "is a freeze, not a setpoint. Re-run before believing it; an app-side SoC "
                     "ceiling, cell balancing or charge taper produces the same trace.")
    elif exported and at_setpoint:
        lines.append("=> HONOURED. The battery took roughly what it was told to take and the "
                     "power it stopped taking went to the grid. Mode 1 is a real setpoint, and "
                     "CAP is confirmed rather than merely consistent.")
    elif not exported and at_baseline:
        lines.append(f"=> IGNORED. The battery carried on taking ~{base_charge:+.0f}W "
                     f"regardless of a {want}W setpoint, and export did not move. Mode 1 is "
                     "accepted and discarded; it is self-consumption under another name.")
        lines.append("   The `self` action should stay a dispatch RELEASE -- a command that "
                     "does nothing is strictly worse than none: same behaviour, one more "
                     "active-block state that reads as a hijack.")
    else:
        charge_says = ("at the setpoint" if at_setpoint else
                       "at the baseline" if at_baseline else
                       "DISCHARGING, which none of the three predicts" if mid_charge < 0 else
                       "in a dead zone between two predictions, matching neither -- which is "
                       "what a battery honouring the sign but clamping the magnitude looks like")
        export_says = ("the setpoint displaced real power" if exported
                       else "nothing was displaced")
        lines.append("=> INCONCLUSIVE. The channels do not agree on one hypothesis: export "
                     f"says {export_says}, and the charge sits {charge_says}. That is the "
                     "signature of something else moving during the window, not of a fourth "
                     "behaviour. Re-run in steadier conditions.")

    base_load = med([house_load(s) for s in baseline])
    during_load = med([house_load(s) for s in during])
    if abs(during_load - base_load) > LOAD_DRIFT:
        lines.append(f"!! house load moved {during_load - base_load:+.0f}W between baseline and "
                     f"command (>{LOAD_DRIFT}W). The expected export is derived from the "
                     "BASELINE charge, so treat the above as weak and re-run.")
    if base_charge < MIN_CHARGE_UNDER:
        lines.append("!! the baseline charge was marginal -- treat all of the above as weak.")
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
        surplus = med([surplus_of(s) for s in baseline])
        base_charge = med([-s["battery_w"] for s in baseline])
        solar_spread = max(s["solar_w"] for s in baseline) - min(s["solar_w"] for s in baseline)

        print(f"\n  SoC {soc}%   surplus ~{surplus:+.0f}W   battery taking ~{base_charge:+.0f}W"
              f"   solar spread {solar_spread}W")

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

        if undercommand and base_charge < MIN_CHARGE_UNDER:
            print(f"\nABORT: the battery is only taking ~{base_charge:+.0f}W of the "
                  f"~{surplus:+.0f}W surplus, below {MIN_CHARGE_UNDER}W. Only power the "
                  "battery is ALREADY taking can be displaced into the export channel, so a "
                  "small baseline charge caps the signal however bright it is. Re-run when "
                  "the battery is charging harder -- lower SoC, or a bigger surplus.")
            return 2

        watts, why = plan_command(base_charge if undercommand else surplus, undercommand)
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
        for line in verdict(baseline, during, watts, aborted, undercommand, dry_run):
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
