"""Reading slots.json and deciding what to command. DESIGN-dispatch.md section 5, steps 1-6.

Pure: no Modbus, no filesystem beyond one read, no wall clock -- `now` is always passed in.
That is what makes the 60 s loop testable at boundary instants (slot start, slot end, one
second either side, gaps, past horizon, stale file) without a simulator and without waiting.

The loop's job is then narrow: call `decide()`, do what it says, report what happened.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from registers import Command, DispatchMode

# The dead man's switch is written 5x longer than the refresh interval. The GAP between them
# is the failsafe margin -- how many ticks may be missed before the inverter reverts on its
# own -- not a knob to shrink for its own sake. At 60/300 the loop can miss four consecutive
# ticks and still hold the command.
REFRESH_INTERVAL_S = 60
DISPATCH_DURATION_S = 300

# A plan older than this is not extrapolated. The planner runs every 3 h, so 4 h means one
# run may be missed or late without the battery falling back; two misses and it does.
MAX_PLAN_AGE = dt.timedelta(hours=4)

# Live SoC has to differ from the target by at least this much for a Mode 2 command to do
# anything. The SoC register steps in 0.4 % and the measurement register reads to 0.1 %, so a
# target within a hair of the current value is a command to do nothing -- which the inverter
# accepts, leaving every monitor green while nothing happens.
SOC_DEADBAND_PCT = 1.0

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


def decide(
    doc: dict | None,
    now: dt.datetime,
    live_soc_pct: float | None,
    load_error: str = "",
) -> Decision:
    """Sections 5.2-5.6, as one pure function.

    `doc` is None when slots.json could not be read at all; `load_error` carries why.
    `live_soc_pct` is None when the SoC register could not be read this tick.
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
        return Decision(
            "command", "hold at 0 W",
            command=Command(DispatchMode.FOLLOW, 0, None, DISPATCH_DURATION_S), slot=slot)

    # charge / discharge -- re-check the direction rule against LIVE SoC, not planned SoC.
    # The plan's trajectory was a forecast made up to three hours ago; the battery is where it
    # actually is. A Mode 2 command whose target sits the wrong side of the real SoC is a
    # silent no-op: accepted, ignored, every monitor green.
    target = float(slot["target_soc"])
    if live_soc_pct is None:
        return Decision("idle", "live SoC unreadable -- cannot check the direction rule",
                        slot=slot)

    if action == "charge":
        if target <= live_soc_pct + SOC_DEADBAND_PCT:
            return Decision(
                "command",
                f"charge target {target:.1f}% not above live SoC {live_soc_pct:.1f}% "
                f"(+{SOC_DEADBAND_PCT}% deadband) -- holding instead",
                command=Command(DispatchMode.FOLLOW, 0, None, DISPATCH_DURATION_S), slot=slot)
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
    """
    charge_ceiling = min(max_charge_w or hard_max_w, hard_max_w)
    discharge_ceiling = min(max_discharge_w or hard_max_w, hard_max_w)

    if cmd.power_w > charge_ceiling > 0:
        return (Command(cmd.mode, charge_ceiling, cmd.target_soc_pct, cmd.duration_s),
                f"charge {cmd.power_w} W exceeds the {charge_ceiling} W ceiling -- clamped")
    if -cmd.power_w > discharge_ceiling > 0:
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
