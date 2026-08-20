"""Reading slots.json and deciding what to command. DESIGN-dispatch.md section 5, steps 1-6.

Pure: no Modbus, no filesystem beyond one read, no wall clock -- `now` is always passed in.
That is what makes the 60 s loop testable at boundary instants (slot start, slot end, one
second either side, gaps, past horizon, stale file) without a simulator and without waiting.

The loop's job is then narrow: call `decide()`, do what it says, report what happened.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from registers import Command, DispatchMode

# The dead man's switch is written 5x longer than the refresh interval. The GAP between them
# is the failsafe margin -- how many ticks may be missed before the inverter reverts on its
# own -- not a knob to shrink for its own sake. At 60/300 the loop can miss four consecutive
# ticks and still hold the command.
REFRESH_INTERVAL_S = 60
DISPATCH_DURATION_S = 300

# A plan older than this is not extrapolated. TWO MISSED RUNS, and the number follows the
# planner's cadence rather than the clock: it runs HOURLY (confirmed 2026-08-20 -- `plan_run`
# lands at :05 past every hour, unbroken over the preceding 30 h), so this is the same "one
# run may be late without the battery falling back, two and it does" rule the original 4 h
# encoded when the planner ran 3-hourly.
#
# The trade is not "strict vs lenient", it is which wrong answer to prefer. Too wide, and a
# dead planner keeps the battery following a plan whose assumptions have expired -- the exact
# failure `dispatch-vs-plan` cannot see, because every register still reads healthy. Too
# narrow, and one late run drops the battery to plain self-consumption, which costs the
# plan's arbitrage but cannot do anything unsafe. The second is the better failure, and it is
# also the reversible one.
MAX_PLAN_AGE = dt.timedelta(hours=2)

# Live SoC has to differ from the target by at least this much for a Mode 2 command to do
# anything. The SoC register steps in 0.4 % and the measurement register reads to 0.1 %, so a
# target within a hair of the current value is a command to do nothing -- which the inverter
# accepts, leaving every monitor green while nothing happens.
#
# ONE REGISTER STEP, not the 1.0 % this started at. The deadband exists to catch a command
# that cannot express itself; it is not a tolerance on the plan. At 27,900 Wh, 1.0 % is 279 Wh
# and 3.5 minutes of a 4,850 W charge, so the old value ENDED EVERY CHARGE EARLY rather than
# suppressing a no-op. MEASURED 2026-08-20: the 15:15 slot commanded 4,848 W to 65.3 %, and at
# 13:24:35Z with the gauge reading 64.4 % the deadband downgraded it to a hold -- 5.5 minutes
# before the slot ended, with 2.6 kW of PV going to the meter for the rest of it.
SOC_DEADBAND_PCT = 0.4

# Generation beyond the house load, above which a hold that would otherwise freeze the battery
# is released to self-consumption instead. See `_charge_target_reached`.
#
# Derived from the two power registers rather than read from the grid meter alone, because
# grid power MOVES WHEN WE ACT: release the battery, the surplus flows into it, the meter
# reads ~0, and a rule keyed on the meter flips back to hold on the next tick -- a 60 s
# oscillation between frozen and released. Generation-minus-load does not move, because
# `load = pv + grid + battery` is an identity (positive grid = import, positive battery =
# discharge), so `surplus = pv - load = -(grid + battery)` is invariant to what the battery
# is doing. Verified against both states of the same afternoon: frozen, grid -2,628 W and
# battery 0 gives 2,628 W; charging, grid +2,371 W and battery -4,823 W gives 2,452 W.
SURPLUS_HARVEST_W = 200.0

# The ceiling `clamp()` will not exceed whatever the inverter says about itself.
#
# MEASURED 2026-08-16, first containerised dry run: the inverter reports 0x012C = 15,015 W
# charge and 0x012D = 13,728 W discharge. Those are not this system's limits -- the SMILE-G3-S5
# is a 5 kW unit and the planner is tuned to maxChargeSpeed=4850 / maxDischargeSpeed=4700. A
# clamp that only fires above 15 kW is decoration: every physically impossible command passes
# it, which is exactly the class of command it exists to stop.
#
# So the clamp takes the LOWER of the two, and this constant is the one that will normally
# bind. Sized a little above the planner's tuning so a legitimate plan is never trimmed
# silently, and far below what the registers claim.
HARD_MAX_POWER_W = 5000

# The safety backstop behind monitor #8 (`soc-floor`, section 6.1). Not a control input --
# nothing here refuses a command because of it, because the direction rule and the plan's own
# reserve already do that. It exists to answer "did the battery end up somewhere it should
# never be", which is a different question from "is any single decision wrong", and it is the
# only monitor that can catch a plan that is internally consistent and still wrong.
#
# Matches the planner's `minBatterySOCPct`. Overridable so the two can be aligned from `.env`
# rather than by editing code in two repos on the same day.
SOC_FLOOR_PCT = float(os.environ.get("SOC_FLOOR_PCT") or 10.0)


class SlotsError(ValueError):
    """slots.json is unreadable or malformed. Always carries the cause -- it reaches a Kuma
    monitor as `status=down`, and "no ping received" is the difference between diagnosing
    from a phone and opening container logs (commit 78c94a9)."""


@dataclass(frozen=True)
class Decision:
    """What the loop should do this tick.

    kind:
      "command" -- write the block and set start=1
      "release" -- write start=0; the plan actively wants plain self-consumption
      "idle"    -- write NOTHING; let the dead man's switch expire (see below)
    """
    kind: str
    reason: str
    command: Command | None = None
    slot: dict | None = None
    fresh: bool = True

    @property
    def dispatching(self) -> bool:
        return self.kind == "command"


def load(path: str | Path) -> dict:
    """Read and validate slots.json. Raises SlotsError with a usable message."""
    p = Path(path)
    try:
        doc = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise SlotsError(f"{p} does not exist -- has the translator ever run?") from e
    except json.JSONDecodeError as e:
        raise SlotsError(f"{p} is not valid JSON: {e}") from e

    for key in ("generated_at", "horizon_end", "slots"):
        if key not in doc:
            raise SlotsError(f"{p} is missing required key {key!r}")
    if not isinstance(doc["slots"], list):
        raise SlotsError(f"{p}: 'slots' must be a list")

    for i, s in enumerate(doc["slots"]):
        for key in ("start", "end", "action"):
            if key not in s:
                raise SlotsError(f"{p}: slot {i} is missing {key!r}")
        if s["action"] in ("charge", "discharge") and (
                s.get("power_w") is None or s.get("target_soc") is None):
            raise SlotsError(f"{p}: slot {i} is a {s['action']} but has no power_w/target_soc")
    return doc


def _parse(ts: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as e:
        raise SlotsError(f"unparseable timestamp {ts!r}: {e}") from e


def freshness(doc: dict, now: dt.datetime, max_age: dt.timedelta = MAX_PLAN_AGE):
    """(is_fresh, reason). Section 5 step 2.

    Two independent ways to be stale, and they mean different things: an old
    `generated_at` means the translator stopped; a passed `horizon_end` means the translator
    is fine but the plan has simply run out. Both end dispatch, and the operator needs to know
    which, so the reason string says so.
    """
    age = now - _parse(doc["generated_at"])
    if age > max_age:
        hrs = age.total_seconds() / 3600
        return False, f"plan is {hrs:.1f} h old (limit {max_age.total_seconds()/3600:.0f} h)"
    if now >= _parse(doc["horizon_end"]):
        return False, f"horizon ended at {doc['horizon_end']}"
    if age < -dt.timedelta(minutes=5):
        # A plan from the future means a clock disagreement between the translator and the
        # dispatcher. Refusing is right: every freshness and slot decision below depends on
        # the two agreeing, so a skewed clock would silently mis-select slots rather than
        # fail. Five minutes of tolerance absorbs ordinary NTP drift.
        return False, f"generated_at {doc['generated_at']} is in the future -- clock skew"
    return True, ""


def find_slot(doc: dict, now: dt.datetime) -> dict | None:
    """The slot covering `now`, half-open [start, end). Earliest match wins on overlap.

    Overlaps are a warning rather than an error per section 4.3 -- the translator should not
    produce them, but a hand-edited file might, and refusing to dispatch at all would be a
    worse failure than picking deterministically.
    """
    for s in doc["slots"]:
        if _parse(s["start"]) <= now < _parse(s["end"]):
            return s
    return None


def _charge_target_reached(slot: dict, target: float, live_soc_pct: float,
                           surplus_w: float | None) -> Decision:
    """A charge slot whose target the battery has already reached.

    THE OLD ANSWER WAS A 0 W HOLD, AND IT GAVE AWAY SOLAR. A hold is Mode 3 at zero, which
    freezes the battery for the rest of the slot; if the sun is producing more than the house
    is using, every watt of that goes to the meter at the sell price. MEASURED 2026-08-20
    13:24:35Z-13:30Z: frozen at 64.4 % against a 65.3 % target while PV made 3.0 kW against a
    0.4 kW load, exporting 2.6 kW for five and a half minutes -- and the following slot then
    bought from the grid to put the same energy back.

    So when there IS surplus generation, release to self-consumption instead. That is the
    same judgement `translator._can_harvest` makes on the plan: the battery absorbing free
    solar is never the wrong answer on an interval the plan wanted CHARGED, and unlike a Mode 2
    command it cannot be a no-op, because self-consumption has no target to have arrived at.

    With no surplus -- night, or a house eating everything it makes -- hold is still right, and
    is still what an unreadable power register falls back to. Releasing there would let the
    battery cover the house load and spend the charge the plan just paid for.
    """
    reason = (f"charge target {target:.1f}% not above live SoC {live_soc_pct:.1f}% "
              f"(+{SOC_DEADBAND_PCT}% deadband)")
    if surplus_w is not None and surplus_w > SURPLUS_HARVEST_W:
        return Decision(
            "release",
            f"{reason} -- releasing to self-consumption to soak up {surplus_w:.0f} W "
            f"of surplus generation",
            slot=slot)
    return Decision(
        "command", f"{reason} -- holding instead",
        command=Command(DispatchMode.FOLLOW, 0, None, DISPATCH_DURATION_S), slot=slot)


def decide(
    doc: dict | None,
    now: dt.datetime,
    live_soc_pct: float | None,
    load_error: str = "",
    surplus_w: float | None = None,
) -> Decision:
    """Sections 5.2-5.6, as one pure function.

    `doc` is None when slots.json could not be read at all; `load_error` carries why.
    `live_soc_pct` is None when the SoC register could not be read this tick.
    `surplus_w` is generation beyond the house load, `-(grid_w + battery_w)`, or None when
    either register could not be read -- see SURPLUS_HARVEST_W.
    """
    if doc is None:
        return Decision("idle", load_error or "no slots loaded", fresh=False)

    fresh, why = freshness(doc, now)
    if not fresh:
        return Decision("idle", why, fresh=False)

    slot = find_slot(doc, now)
    if slot is None:
        return Decision("idle", f"no slot covers {now.isoformat()}", slot=None)

    action = slot["action"]

    if action == "self":
        # Plain self-consumption is what the plan wants here, and that is the absence of a
        # command rather than a mode. Released promptly rather than left to expire: the plan
        # asked for it now, not in up to five minutes.
        return Decision("release", "plan wants self-consumption", slot=slot)

    if action == "hold":
        # THE PLAN'S OWN HOLD, OVERRIDDEN ONLY BY MEASURED SURPLUS. The plan holds when it
        # forecasts the battery neither charging nor discharging, and freezing is right
        # whenever it is protecting its inventory. But the forecast that decides this is the
        # one the archive shows to be least reliable: PV runs ~2.3x low in the late afternoon
        # (see `translator._can_harvest`), so a hold with a near-zero export forecast is
        # routinely a hold with kilowatts actually spilling.
        #
        # MEASURED 2026-08-20: the 15:00 slot planned a 106 Wh export and exported ~675 Wh,
        # and the 15:15 slot then BOUGHT 1 kWh from the grid to put the same energy back.
        # Feed-in that quarter paid EUR 0.079/kWh; the buys the plan makes later that
        # afternoon cost EUR 0.23-0.32/kWh all-in. Across the day 4.8 kWh left the meter with
        # the battery frozen below 98 %.
        #
        # The override cannot cost money, which is what makes it safe to make against the
        # optimiser: a release has no setpoint, so it can only absorb generation that already
        # exists. The one asymmetry worth naming -- keeping headroom for a cheaper charge
        # later -- does not arise on this tariff: the all-in buy price has not gone negative
        # once in 45 days (the energy tax floors it), while the raw market price went negative
        # in 137 quarter-hours, i.e. intervals where exporting COSTS money and absorbing is
        # more right rather than less.
        if surplus_w is not None and surplus_w > SURPLUS_HARVEST_W:
            # If the sun goes behind a cloud a second after this, self-consumption covers the
            # house from the battery until the next tick reverts to the freeze. That is one
            # tick, 60 s, tens of Wh -- against the kWh/day above.
            return Decision(
                "release",
                f"plan wants a hold, but {surplus_w:.0f} W of surplus generation is going to "
                f"the meter -- releasing to self-consumption to soak it up",
                slot=slot)
        return Decision(
            "command", "hold at 0 W",
            command=Command(DispatchMode.FOLLOW, 0, None, DISPATCH_DURATION_S), slot=slot)

    # charge / discharge -- re-check the direction rule against LIVE SoC, not planned SoC.
    # The plan's trajectory was a forecast made up to an hour ago; the battery is where it
    # actually is. A Mode 2 command whose target sits the wrong side of the real SoC is a
    # silent no-op: accepted, ignored, every monitor green.
    target = float(slot["target_soc"])
    if live_soc_pct is None:
        return Decision("idle", "live SoC unreadable -- cannot check the direction rule",
                        slot=slot)

    if action == "charge":
        if target <= live_soc_pct + SOC_DEADBAND_PCT:
            return _charge_target_reached(slot, target, live_soc_pct, surplus_w)
        power = int(slot["power_w"])            # charging-positive
    else:
        if target >= live_soc_pct - SOC_DEADBAND_PCT:
            return Decision(
                "command",
                f"discharge target {target:.1f}% not below live SoC {live_soc_pct:.1f}% "
                f"(-{SOC_DEADBAND_PCT}% deadband) -- holding instead",
                command=Command(DispatchMode.FOLLOW, 0, None, DISPATCH_DURATION_S), slot=slot)
        power = -int(slot["power_w"])           # discharging is negative in our convention

    return Decision(
        "command", f"{action} {abs(power)} W to {target:.1f}%",
        command=Command(DispatchMode.SOC_TARGET, power, target, DISPATCH_DURATION_S),
        slot=slot)


def clamp(cmd: Command, max_charge_w: int | None, max_discharge_w: int | None,
          hard_max_w: int = HARD_MAX_POWER_W):
    """Clamp to the lower of the inverter's own limits (0x012C / 0x012D) and `hard_max_w`.

    Reading the ceiling from the hardware means it cannot drift from the hardware, and that
    was the whole argument for doing it that way -- but the hardware turns out to overstate
    itself by roughly threefold (see HARD_MAX_POWER_W), so on its own it clamps nothing.
    Taking the lower of the two keeps the hardware in the loop for the case where it reports
    something LOWER than expected -- a derate, a firmware change, a different unit -- while
    still refusing a command this system cannot physically execute.

    A clamp firing never means "the inverter is the limit". It means the plan asked for
    something it should never have asked for, which is worth saying loudly rather than
    silently satisfying.

    `None` means the limit registers could not be read this run, and falls back to
    `hard_max_w`. **Zero does not mean None.** A limit register reading 0 is the inverter
    refusing that direction outright -- a derate, a fault, a pack at its own limit -- and the
    only safe response is to stop asking. Treating the two the same is how a "clamp" ends up
    commanding 5 kW into a battery that just said it would accept nothing.
    """
    charge_ceiling = hard_max_w if max_charge_w is None else min(max_charge_w, hard_max_w)
    discharge_ceiling = (
        hard_max_w if max_discharge_w is None else min(max_discharge_w, hard_max_w))

    if cmd.power_w > 0 and charge_ceiling == 0:
        return (Command(DispatchMode.FOLLOW, 0, None, cmd.duration_s),
                f"the inverter reports 0 W max charge -- refusing the {cmd.power_w} W charge "
                f"and holding instead")
    if cmd.power_w < 0 and discharge_ceiling == 0:
        return (Command(DispatchMode.FOLLOW, 0, None, cmd.duration_s),
                f"the inverter reports 0 W max discharge -- refusing the {-cmd.power_w} W "
                f"discharge and holding instead")

    if cmd.power_w > charge_ceiling:
        return (Command(cmd.mode, charge_ceiling, cmd.target_soc_pct, cmd.duration_s),
                f"charge {cmd.power_w} W exceeds the {charge_ceiling} W ceiling -- clamped")
    if -cmd.power_w > discharge_ceiling:
        return (Command(cmd.mode, -discharge_ceiling, cmd.target_soc_pct, cmd.duration_s),
                f"discharge {-cmd.power_w} W exceeds the {discharge_ceiling} W ceiling "
                f"-- clamped")
    return cmd, ""


def matches_command(state: dict, cmd: Command) -> bool:
    """Does the dispatch block hold exactly what `cmd` asked for?

    Two callers with opposite intentions -- `is_hijacked()` asks because a mismatch means
    something ELSE wrote the block, and the loop's verify step asks because a mismatch means
    OUR OWN write did not land. Same comparison, so it lives in one place; the two readings
    of a False are the callers' business.

    Duration is deliberately not compared: it counts down, and section 5.1 records that it
    does so erratically -- observed reading 300 s three times across two minutes, then
    straight to expiry.

    Nor is the SoC register compared when the command did not write one. A Mode 3 hold leaves
    whatever 0x0886 held before, so that value is not evidence of anything.
    """
    if state["mode"] != cmd.mode:
        return False
    if state["power_w"] != cmd.power_w:
        return False
    if cmd.target_soc_pct is not None:
        # 0.4 %/bit -- a readback cannot be more precise than the step it was written at.
        return abs(state["target_soc_pct"] - cmd.target_soc_pct) <= 0.4
    return True


def is_hijacked(state: dict, last_written: Command | None) -> bool:
    """Section 5 step 5: is something else driving the dispatch block?

    True when the block is active but does not match what this process last wrote. The
    AlphaESS app writes these same registers -- caught on 2026-08-15 16:11 holding
    `mode=2 dpwr=-5000W dsoc=100.0% dt=5580s`, a 93-minute grid force-charge.
    """
    if not state.get("dispatch_active"):
        return False
    if last_written is None:
        return True
    return not matches_command(state, last_written)
