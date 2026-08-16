"""Register map and encodings for the AlphaESS SMILE-G3-S5 dispatch block.

Pure functions only: no I/O, no pymodbus import, no clock. Everything here is a total
function of its arguments, so the whole module is testable without hardware and without a
simulator. The transport lives in scheduler.py.

This is the layer that gets silently wrong. A bad encoding does not raise -- it writes a
plausible number to a real inverter and the battery does something unintended for the next
fifteen minutes. Two habits guard against that:

  - every constant carries its hex address in a comment, because the PDFs are indexed by hex
    and the wire is indexed by decimal, and the one time that translation was done from
    memory it produced 0x0883 for the mode register (0x0883 is reactive power; mode is
    0x0885)
  - encode/decode come in pairs and are round-trip tested

SIGN CONVENTIONS, which differ by layer and are the other thing that gets confused:

  register `active power`   >0 discharge, <0 charge, offset by +32000
  register `battery_power`  >0 discharging, <0 charging
  this module's Command     >0 CHARGING, <0 discharging

The last one is the odd one out and it is deliberate. Every dashboard panel in this repo
counts charging positive -- `grafana/generate-battery-plan.py` panel 9 negates
`battery_power_w` in Flux specifically to achieve that, and says so in its description. A
dispatch_state series that disagreed in sign with the actual-power series beside it would be
worse than no series at all. So the flip happens once, here, next to the constant that
causes it, rather than being re-derived in Flux by whoever adds the next panel.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

# --- dispatch block, 0x0880-0x0888 -------------------------------------------------------
# Nine registers. The official spec writes all nine in one FC 0x10 transaction; see
# DESIGN-dispatch.md section 9 open question 5 for why we currently do not.
REG_START = 2176           # 0x0880  1 word,  0 = release, 1 = dispatch active
REG_POWER = 2177           # 0x0881  2 words, active power, POWER_OFFSET applied
REG_REACTIVE = 2179        # 0x0883  2 words, reactive power. Never written; documented so
                           #         nobody mistakes this address for the mode register.
#
# On the word counts of 0x0881 and 0x0883: alphaess_modbus_register_reference.csv lists both
# as 1 word and marks active power `signed`. Both are wrong, and the register map itself is
# the proof -- reactive power sits at 0x0883, two addresses above active power, which only
# works if active power occupies 0x0881-0x0882. A 1-word active power would put reactive at
# 0x0882. The verified 2026-08-15 run read and wrote both as 2 words and produced values
# matching observed battery behaviour.
#
# `signed` is wrong for a different reason: the +32000 offset IS the sign mechanism, so the
# raw value is unsigned and the offset carries the direction. Reading it signed works by
# accident for every value under 32767 and breaks silently above it -- i.e. for any discharge
# command over 767 W. Treat that CSV as good-quality community inference, not as ground truth;
# where it disagrees with the official PDFs or with observed behaviour, it loses.
REG_MODE = 2181            # 0x0885  1 word,  see DispatchMode
REG_SOC = 2182             # 0x0886  1 word,  raw = pct / SOC_STEP
REG_TIME = 2183            # 0x0887  2 words, seconds -- the dead man's switch

DISPATCH_BLOCK = (REG_START, 9)  # (start address, word count) for a whole-block read

# --- measurement -------------------------------------------------------------------------
REG_GRID_POWER = 33        # 0x0021  2 words, signed, W. Positive = importing.
REG_BATTERY_SOC = 258      # 0x0102  1 word,  raw / 10 -> %
REG_BATTERY_POWER = 294    # 0x0126  1 word,  signed, W. Positive = discharging.
REG_PV_METER = 161         # 0x00A1  2 words, signed, W. AC-coupled PV meter.
                           #         NOT pv1_power..pv4_power: those are DC MPPT registers
                           #         and read 0 forever on this site, which is an
                           #         AC-coupled install behind APsystems micro-inverters.

# --- inverter limits ---------------------------------------------------------------------
# The authoritative answer to "how hard may we push". Read from the inverter rather than
# configured, so they cannot drift from the hardware the way a copied constant would.
#
# These are a nameplate ceiling and will usually be ABOVE the planner's tuned figures
# (maxChargeSpeed=4850 / maxDischargeSpeed=4700, which are measured p99s). Clamping to these
# is a safety net against sending an out-of-range value; it is not a substitute for the plan
# respecting its own limits. A slot that needs clamping here means the plan asked for
# something the battery cannot do, which is worth logging loudly.
REG_MAX_CHARGE_POWER = 300      # 0x012C  1 word, W
REG_MAX_DISCHARGE_POWER = 301   # 0x012D  1 word, W

POWER_OFFSET = 32000
# 0.4 %/bit, not the widely-repeated community figure of 0.392.
#
# The discriminating evidence is the app's force-charge of 2026-08-15 16:11: it displayed
# 100% and 0x0886 read 250. 250 * 0.4 = 100.0 exactly; 250 * 0.392 = 98.0, which the app
# would not have shown as 100.
#
# The other observation often cited for this -- 0x0886 holding 27 against a configured
# "discharge to 11%" -- proves nothing either way, because 27 * 0.4 = 10.8 and
# 27 * 0.392 = 10.58 both round to 11. Do not treat it as confirmation.
SOC_STEP = 0.4


class DispatchMode:
    """Only the modes with observed behaviour are named."""
    PV_CHARGE = 1      # "battery charges from PV only, discharge forbidden".
                       # At power == +0 (raw 32000) it holds flat and exports surplus,
                       # indistinguishable from FOLLOW. Negative-power behaviour is
                       # UNTESTED -- see dispatch/test_mode1_negative.py.
    SOC_TARGET = 2     # The workhorse. Honours power setpoint and SoC target.
    FOLLOW = 3         # "Load following". At power == +0 it freezes the battery and
                       # exports surplus. This is the verified hold.

    NAMES: ClassVar[dict[int, str]] = {
        1: "PV charge", 2: "SoC control", 3: "load following",
    }


@dataclass(frozen=True)
class Command:
    """One dispatch command, in human/dashboard units.

    power_w is positive for CHARGING (see module docstring). target_soc_pct is where the
    battery should end up; duration_s is the dead man's switch.
    """
    mode: int
    power_w: int
    target_soc_pct: float | None
    duration_s: int

    def __post_init__(self):
        if self.mode not in DispatchMode.NAMES:
            raise ValueError(f"unknown dispatch mode {self.mode}")
        # None means "leave the SoC register alone", which is what a Mode 3 hold wants: it
        # freezes the battery where it is, so a target would be meaningless, and writing one
        # would leave a stale number for the next reader to misinterpret.
        if self.target_soc_pct is not None and not 0 <= self.target_soc_pct <= 100:
            raise ValueError(f"target_soc_pct out of range: {self.target_soc_pct}")
        if self.mode == DispatchMode.SOC_TARGET and self.target_soc_pct is None:
            raise ValueError("mode 2 is SoC control -- it requires a target")
        if self.duration_s <= 0:
            # A zero duration is not "forever", it is a command that expires immediately.
            # Refusing it here turns a silent no-op into a loud one.
            raise ValueError(f"duration_s must be positive, got {self.duration_s}")


def encode_int32(value: int) -> list[int]:
    """Big-endian 32-bit split into two 16-bit registers, two's complement."""
    value &= 0xFFFFFFFF
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def decode(regs: list[int], signed: bool = False) -> int:
    """One or two registers to an int. Two-register values are big-endian."""
    if len(regs) == 1:
        value, bits = regs[0], 16
    elif len(regs) == 2:
        value, bits = (regs[0] << 16) | regs[1], 32
    else:
        raise ValueError(f"decode expects 1 or 2 registers, got {len(regs)}")
    if signed and value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def encode_power(power_w: int) -> list[int]:
    """Charging-positive watts -> the two POWER registers.

    Flips sign into the register's discharge-positive convention, then applies the offset.
    """
    raw = POWER_OFFSET - power_w
    if not 0 <= raw <= 0xFFFFFFFF:
        raise ValueError(f"power {power_w} W encodes out of range")
    return encode_int32(raw)


def decode_power(regs: list[int]) -> int:
    """The two POWER registers -> charging-positive watts. Inverse of encode_power."""
    return POWER_OFFSET - decode(regs)


def encode_soc(pct: float) -> list[int]:
    """Percent -> the SoC target register.

    Rounds to the nearest representable step. 0.4 %/bit means not every percentage is
    expressible; 78.0 lands exactly on 195, but 78.1 does not exist and becomes 78.0.
    """
    raw = round(pct / SOC_STEP)
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"SoC {pct}% encodes out of range")
    return [raw]


def decode_soc(regs: list[int]) -> float:
    """The SoC target register -> percent. Not an exact inverse of encode_soc when the
    input was not on a 0.4 boundary; that lossiness is in the hardware, not here."""
    return round(decode(regs) * SOC_STEP, 1)


def encode_command(cmd: Command) -> dict[int, list[int]]:
    """A Command -> {address: [words]}, ready to write.

    Returns a mapping rather than issuing writes so the caller owns transport and ordering.
    START is deliberately absent: the caller writes the payload first and START last, so a
    partially-written command is never live. scheduler.py enforces that ordering.
    """
    writes = {
        REG_MODE: [cmd.mode],
        REG_POWER: encode_power(cmd.power_w),
        REG_TIME: encode_int32(cmd.duration_s),
    }
    if cmd.target_soc_pct is not None:
        writes[REG_SOC] = encode_soc(cmd.target_soc_pct)
    return writes


def decode_block(words: list[int]) -> dict:
    """A 9-word read of DISPATCH_BLOCK -> decoded state.

    Returned in dashboard units, ready to become a `dispatch_state` point. Kept separate
    from Command because a readback can hold values no Command would produce -- notably
    whatever the AlphaESS app last wrote.
    """
    if len(words) != DISPATCH_BLOCK[1]:
        raise ValueError(f"expected {DISPATCH_BLOCK[1]} words, got {len(words)}")
    base = REG_START
    def at(addr, n=1):
        i = addr - base
        return words[i:i + n]

    mode = decode(at(REG_MODE))
    return {
        "dispatch_active": decode(at(REG_START)),
        "mode": mode,
        "mode_name": DispatchMode.NAMES.get(mode, f"unknown ({mode})"),
        "power_w": decode_power(at(REG_POWER, 2)),
        "target_soc_pct": decode_soc(at(REG_SOC)),
        "duration_s": decode(at(REG_TIME, 2)),
    }


def describe(state: dict) -> list[tuple[str, str, int, str]]:
    """Decoded state -> rows of (hex address, name, raw, meaning) for the dashboard table.

    The raw column stays alongside the meaning on purpose: half the value of that panel is
    checking a decode against the spec without leaving the dashboard. The 0.392 %/bit error
    propagated through the community precisely because nobody could see both at once.
    """
    active = state["dispatch_active"]
    power = state["power_w"]
    if power > 0:
        power_note = f"{power:+d} W (charging at {power / 1000:.1f} kW)"
    elif power < 0:
        power_note = f"{power:+d} W (discharging at {abs(power) / 1000:.1f} kW)"
    else:
        power_note = "0 W (hold)"
    return [
        ("0x0880", "Dispatch start", active, "Active" if active else "Released"),
        ("0x0881", "Active power", decode(encode_power(power)), power_note),
        ("0x0885", "Mode", state["mode"], state["mode_name"]),
        ("0x0886", "SoC target", round(state["target_soc_pct"] / SOC_STEP),
         f"{state['target_soc_pct']:.1f} %"),
        ("0x0887", "Duration", state["duration_s"],
         f"{state['duration_s'] // 60} min {state['duration_s'] % 60} s remaining"),
    ]
