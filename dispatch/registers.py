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

# --- battery cell voltage, 0x0105-0x010A --------------------------------------------------
# Six contiguous registers, immediately below the temperature block and read as its own
# separate block (see TEMP_BLOCK below) -- same fleet-extremes shape, pack/cell/value-times-two.
#
# THE ADDRESS IS 0x0105, NOT 0x0106. `registers.py` carried a comment here (see TODO.md #12,
# now resolved) citing Alpha2MQTT for `0x0106`-`0x010A`, 5 words -- the same comment that
# diagnosed the temp block's OWN scale as a copy-paste from this one. That 5-word claim is
# short by exactly one register: AlphaESS's own "Parameter address table" (via
# `ha-alphaess-modbus`) lists six registers here, pack ID/cell ID/value twice over, and its
# boundary lines up exactly with this repo's own 2026-08-27 live confirmation of TEMP_BLOCK at
# 0x010B -- the six registers immediately below it can only start at 0x0105. Scale is
# `0.001V/bit`, which Alpha2MQTT and the official table agree on even though they disagree on
# the start address.
REG_MIN_CELL_VOLTAGE_PACK = 261   # 0x0105  1 word,  unsigned, pack holding the lowest cell
REG_MIN_CELL_VOLTAGE_CELL = 262   # 0x0106  1 word,  unsigned, cell within that pack
REG_MIN_CELL_VOLTAGE = 263        # 0x0107  1 word,  unsigned, raw / 1000 -> volts
REG_MAX_CELL_VOLTAGE_PACK = 264   # 0x0108  1 word,  unsigned, pack holding the highest cell
REG_MAX_CELL_VOLTAGE_CELL = 265   # 0x0109  1 word,  unsigned, cell within that pack
REG_MAX_CELL_VOLTAGE = 266        # 0x010A  1 word,  unsigned, raw / 1000 -> volts

VOLTAGE_BLOCK = (REG_MIN_CELL_VOLTAGE_PACK, 6)

# Wide on purpose, same argument as TEMP_PLAUSIBLE_C: not a health threshold, a decode check.
# A scale wrong by a factor of ten lands far outside it, and so does a genuinely dead cell.
VOLTAGE_PLAUSIBLE_V = (1.0, 5.0)

# --- battery cell temperature, 0x010B-0x0110 ---------------------------------------------
# Six contiguous registers, read as one block alongside the measurements above.
#
# THESE ARE FLEET EXTREMES, NOT PER-PACK READINGS. There are three packs here; the block
# reports the single coldest cell and the single hottest cell across all of them, each tagged
# with the pack and cell that produced it. It cannot be fanned out into pack_1/pack_2/pack_3
# temperatures -- that needs the extended per-pack register set, which this site has not
# probed. A panel or field named "pack 2 temp" would be claiming a series that does not exist.
#
# THE TEMPERATURES ARE SIGNED, THE IDs ARE NOT. Alpha2MQTT types 0x010D and 0x0110 as `Short`
# and the four ID registers as `Unsigned Short`, which is the same split the naive decode gets
# wrong: read unsigned, a cell at -0.1 C publishes as +6553.5 C.
#
# ON THE SCALE. Alpha2MQTT comments all six as `0.001D/bit`, which is a copy-paste of the cell
# VOLTAGE block immediately above them (0x0105-0x010A, `0.001V/bit`): at 0.001 a signed 16-bit
# register would top out at 32.7 C, which is not a range anyone specs a battery over.
# `ha-alphaess-modbus` documents these as int16 x0.1 C, and that is what is used here. Same
# standing as REG_POWER's word count above -- community inference, not ground truth -- so
# `temps_plausible` below is the guard, and the first live read is the confirmation.
REG_MIN_CELL_TEMP_PACK = 267   # 0x010B  1 word,  unsigned, pack holding the coldest cell
REG_MIN_CELL_TEMP_CELL = 268   # 0x010C  1 word,  unsigned, cell within that pack
REG_MIN_CELL_TEMP = 269        # 0x010D  1 word,  SIGNED, raw / 10 -> degrees C
REG_MAX_CELL_TEMP_PACK = 270   # 0x010E  1 word,  unsigned, pack holding the hottest cell
REG_MAX_CELL_TEMP_CELL = 271   # 0x010F  1 word,  unsigned, cell within that pack
REG_MAX_CELL_TEMP = 272        # 0x0110  1 word,  SIGNED, raw / 10 -> degrees C

TEMP_BLOCK = (REG_MIN_CELL_TEMP_PACK, 6)  # mirrors DISPATCH_BLOCK: (start address, words)

# What a cell in a house battery can plausibly read. Wide on purpose: this is not a health
# threshold, it is a decode check. A scale wrong by a factor of ten or a hundred lands far
# outside it, and so does an unsigned decode of a sub-zero cell (+6553.5).
TEMP_PLAUSIBLE_C = (-30.0, 80.0)

# Pack IDs are 1-BASED, which is what makes them the tell for a dead block: a zero-filled
# read decodes to a perfectly plausible 0.0 C, and 0.0 C is the exact lie this module refuses
# to publish everywhere else. VERIFIED 2026-08-27 against the live inverter -- the coldest
# cell sat in pack 3 and the hottest in pack 1 on this three-pack site, so 0 is not an ID this
# hardware reports.
#
# The upper bound is loose rather than 3. Three packs is a fact about this site today, not
# about the register, and a fourth pack must not silence a field that is working; 8 still
# rejects the shapes a misread produces -- 0, 65535, a temperature word landing in an ID slot.
PACK_ID_RANGE = (1, 8)

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

# --- health poller: fault/warning block, 0x0131-0x0148 -----------------------------------
# Twenty-four contiguous registers, a few addresses past the inverter limits above -- 0x012E,
# 0x012F and 0x0130 sit unused in between, not part of this block. AlphaESS's own "Parameter
# address table" gives the shape: twelve 32-bit values, two words each, fault1-6 followed by
# warning1-6, and nothing else interleaved. That is the whole of the confirmation -- WHICH BIT
# MEANS WHAT IS STILL UNDOCUMENTED, so no fault is named here, and every word is republished
# raw, keyed by its own hex address, so a real event is locatable even though it is not
# interpretable.
#
# THE BLOCK WAS 22 WORDS UNTIL 2026-09-02, which covered fault1-6 and warning1-5 and stopped
# one 32-bit value short: warning6 (0x0147-0x0148) was never read at all. The count below is
# the reason that mattered enough to fix rather than note -- a summary computed over a block
# that silently omits a word is the kind of confident wrongness this module exists to avoid.
REG_FAULT_WARNING_START = 305   # 0x0131  24 words: fault1-6, then warning1-6, 2 words each
FAULT_BLOCK = (REG_FAULT_WARNING_START, 24)

# Where warning1 starts within the block, as a word offset: six 32-bit faults ahead of it.
# The faults and the warnings are counted separately and that split is the point -- see
# `decode_fault_block`.
FAULT_WORDS = 12

# --- health poller: daily tier, 0x011B / 0x0120-0x0125 / 0x0435 / 0x08D0 ----------------
# State of health, three lifetime energy counters, the inverter's own heatsink temperature,
# and lifetime PV. Slow-moving by nature -- a lifetime counter that moved measurably within an
# hour would be news -- so these get their own DAILY gate rather than riding the hourly one.
#
# EVERY ADDRESS HERE WAS CONFIRMED BY A LIVE READ ON 2026-09-03, and the reason that mattered
# is the module docstring's 0x0883 story in a new form. Two documentary sources disagreed by
# exactly one register: `senalse/ha-alphaess-modbus`'s `const.py` addresses a 32-bit value at
# its FIRST word, while this repo's 2026-08-28 reading of AlphaESS's own parameter table put
# it at the SECOND. `scripts/read-daily-health-registers.py` printed both alignments side by
# side against the live inverter and only one survived:
#
#     0x011F  0            0x0120  10481  ->  1048.1 kWh charge
#     0x0121  686882816    0x0122  10221  ->  1022.1 kWh discharge
#     0x0123  669843456    0x0124   5811  ->   581.1 kWh grid-charge
#     0x0434  0            0x0435    370  ->     37.0 C
#
# The rejected column is not merely wrong, it is wrong in the diagnostic way: 686882816 is one
# counter's low word spliced onto the next one's high word. So const.py's convention wins, the
# 2026-08-28 "correction" was itself the error, and the addresses below are the first-word ones.
#
# THE SCALE IS CONFIRMED TWICE OVER, independently of the addresses. Battery capacity 0x0119
# read 279 in the same run, and this site's battery is 27.9 kWh -- a fact this repo already had
# from the planner, not from any register document. Lifetime PV at 0x08D0 read 86711, which is
# 8671.1 kWh at the same 0.1 kWh/bit: right for an array of this age, where 1 kWh/bit would
# claim 86 MWh. Two unrelated registers agreeing on 0.1 is what makes the scale an observation
# rather than a third document.
REG_SOH = 283                    # 0x011B  1 word,  raw / 10 -> %
REG_LIFETIME_CHARGE = 288        # 0x0120  2 words, raw / 10 -> kWh
REG_LIFETIME_DISCHARGE = 290     # 0x0122  2 words, raw / 10 -> kWh
REG_LIFETIME_GRID_CHARGE = 292   # 0x0124  2 words, raw / 10 -> kWh

# One read spanning 0x011B-0x0125, not four. The three counters run gapless into 0x0126
# (battery power, confirmed live long before this), which is the shape that told us the
# alignment was right; reading them as one block keeps that adjacency visible and costs one
# Modbus round-trip instead of four. 0x011C-0x011F are read and discarded -- they were all
# zero in the confirming run and nothing here claims to know what they are.
DAILY_BATTERY_BLOCK = (REG_SOH, 11)

# 0.1 kWh/bit and 0.1 %/bit. Named rather than inlined because the same figure is applied to
# four different counters and to lifetime PV, and a scale silently differing between two of
# them is the exact class of error the confirming run above exists to rule out.
ENERGY_STEP_KWH = 0.1
SOH_STEP_PCT = 0.1

REG_INVERTER_TEMP = 1077         # 0x0435  1 word,  SIGNED, raw / 10 -> degrees C
DAILY_INVERTER_BLOCK = (REG_INVERTER_TEMP, 1)

# Lifetime PV energy, 2 words at 0x08D0. NOT in `const.py` at all, which lists nothing between
# 0x08D0 and 0x08D4 -- so unlike everything else in this section, the live read is the only
# evidence there is for the address, and the magnitude is the only evidence for its meaning.
#
# 0x08D2 HOLDS THE SAME VALUE. The confirming run read `0001 52b7 0001 52b7` across
# 0x08D0-0x08D3: two identical 32-bit values. Only the first is published. They are NOT
# required to agree -- a decode that failed whenever they diverged would silence the field the
# moment the second one turns out to be a related-but-different total (feed-in, say), which is
# the more likely explanation than a genuine duplicate.
#
# THIS SITE'S PV IS AC-COUPLED, behind APsystems micro-inverters (see REG_PV_METER), which is
# why the INVERTER's own lifetime PV registers at 0x043D-0x043F are not read here: the whole
# 0x0430-0x0440 window read zero in the confirming run except the heatsink temperature. That
# is not an alignment question -- both candidate addresses read 0 -- it is a register this
# install does not populate.
REG_LIFETIME_PV = 2256           # 0x08D0  2 words, raw / 10 -> kWh
DAILY_PV_BLOCK = (REG_LIFETIME_PV, 2)

# Decode checks, not health thresholds -- same standing as TEMP_PLAUSIBLE_C and wide for the
# same reason. What they have to catch is a scale wrong by a factor of ten and the misaligned
# read above, which lands at 68,688,281 kWh.
#
# ZERO IS REJECTED FOR ALL FOUR ENERGY COUNTERS AND FOR SoH, and that is the load-bearing part.
# An unsupported register range, a proxy padding a short reply, and the losing alignment above
# all produce zero words, and zero is otherwise perfectly in range -- it is this tier's version
# of the all-zero temp block that PACK_ID_RANGE exists to catch, except there are no 1-based
# IDs here to break the tie. The cost is that a battery on its commissioning day cannot publish
# a lifetime total, which is a day this site is nearly two thousand cycles past.
SOH_PLAUSIBLE_PCT = (20.0, 110.0)
LIFETIME_ENERGY_PLAUSIBLE_KWH = (0.1, 1_000_000.0)

# Wider than TEMP_PLAUSIBLE_C on the hot side: this is a heatsink, not a cell, and a derating
# inverter can sit well above anything a pack would survive.
#
# The lower bound is 1.0 C rather than -30 for the zero-block reason above, and it is the one
# bound here that gives something up: a genuine 0.0 C heatsink would be discarded as unread.
# An inverter that is answering Modbus is an inverter that is powered and dissipating, indoors,
# so that reading is not one this site can produce -- but it is an assumption, unlike the
# energy counters where zero is contradicted by the site's own history.
INVERTER_TEMP_PLAUSIBLE_C = (1.0, 100.0)

# --- health poller: weekly tripwire blocks ------------------------------------------------
# Firmware, serial numbers, and system configuration barely change -- a missed read costs
# nothing here, unlike the hourly/tick tiers above. Like the fault block, these are republished
# as raw words rather than decoded fields: which word within each block holds which named value
# (BMU firmware version, max feed-into-grid %, ...) is not confirmed against any reference this
# repo has, so inventing field boundaries risks the same "translation from memory" mistake this
# module's docstring opens with. See TODO.md for the follow-up that decodes these properly.
REG_BMU_FW_START = 277           # 0x0115  6 words: BMU/LMU/ISO firmware, battery
                                  #         number/capacity/type
FIRMWARE_BLOCK = (REG_BMU_FW_START, 6)

REG_INVERTER_FW_START = 1600     # 0x0640  20 words: inverter master/slave firmware + serial
INVERTER_FW_BLOCK = (REG_INVERTER_FW_START, 20)

REG_SYSTEM_CONFIG_START = 2048   # 0x0800  16 words: max feed %, PV capacity settings, system
                                  #         mode, battery-ready flag
SYSTEM_CONFIG_BLOCK = (REG_SYSTEM_CONFIG_START, 16)


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


def decode_voltage_block(words: list[int]) -> dict:
    """A 6-word read of VOLTAGE_BLOCK -> decoded battery cell-voltage state.

    All six words are unsigned -- unlike the temp block, there is no signed/unsigned split to
    get wrong here, only the scale. See the block comment above VOLTAGE_BLOCK.
    """
    if len(words) != VOLTAGE_BLOCK[1]:
        raise ValueError(f"expected {VOLTAGE_BLOCK[1]} words, got {len(words)}")
    base = VOLTAGE_BLOCK[0]
    def at(addr, n=1):
        i = addr - base
        return words[i:i + n]

    return {
        "min_cell_voltage_v": round(decode(at(REG_MIN_CELL_VOLTAGE)) * 0.001, 3),
        "min_cell_voltage_pack": decode(at(REG_MIN_CELL_VOLTAGE_PACK)),
        "min_cell_voltage_cell": decode(at(REG_MIN_CELL_VOLTAGE_CELL)),
        "max_cell_voltage_v": round(decode(at(REG_MAX_CELL_VOLTAGE)) * 0.001, 3),
        "max_cell_voltage_pack": decode(at(REG_MAX_CELL_VOLTAGE_PACK)),
        "max_cell_voltage_cell": decode(at(REG_MAX_CELL_VOLTAGE_CELL)),
    }


def voltage_plausible(voltages: dict) -> bool:
    """False when a decoded voltage block cannot be describing this battery.

    Same three-part shape as `temps_plausible` -- range, pack ID, min <= max -- and the same
    reason for the pack-ID check: an all-zero block decodes to a perfectly in-range-looking
    0.0 V in pack 0, and 0 is not an ID this hardware reports (PACK_ID_RANGE).
    """
    lo, hi = VOLTAGE_PLAUSIBLE_V
    pack_lo, pack_hi = PACK_ID_RANGE
    return (lo <= voltages["min_cell_voltage_v"] <= hi
            and lo <= voltages["max_cell_voltage_v"] <= hi
            and voltages["min_cell_voltage_v"] <= voltages["max_cell_voltage_v"]
            and pack_lo <= voltages["min_cell_voltage_pack"] <= pack_hi
            and pack_lo <= voltages["max_cell_voltage_pack"] <= pack_hi)


def decode_temp_block(words: list[int]) -> dict:
    """A 6-word read of TEMP_BLOCK -> decoded battery temperature state.

    Every word goes through `decode` with an explicit `signed`, rather than being indexed and
    multiplied in place: the signedness differs across the block -- temperatures signed, pack
    and cell IDs not -- and it is the one thing about these registers that a reader cannot
    check by eye. See the block comment above TEMP_BLOCK.
    """
    if len(words) != TEMP_BLOCK[1]:
        raise ValueError(f"expected {TEMP_BLOCK[1]} words, got {len(words)}")
    base = TEMP_BLOCK[0]
    def at(addr, n=1):
        i = addr - base
        return words[i:i + n]

    return {
        "min_cell_temp_c": round(decode(at(REG_MIN_CELL_TEMP), signed=True) * 0.1, 1),
        "min_cell_temp_pack": decode(at(REG_MIN_CELL_TEMP_PACK)),
        "min_cell_temp_cell": decode(at(REG_MIN_CELL_TEMP_CELL)),
        "max_cell_temp_c": round(decode(at(REG_MAX_CELL_TEMP), signed=True) * 0.1, 1),
        "max_cell_temp_pack": decode(at(REG_MAX_CELL_TEMP_PACK)),
        "max_cell_temp_cell": decode(at(REG_MAX_CELL_TEMP_CELL)),
    }


def temps_plausible(temps: dict) -> bool:
    """False when a decoded temp block cannot be describing this battery.

    The same argument as `scheduler`'s IMPLAUSIBLE_POWER_W guard on grid and battery power:
    these registers' scale is documented rather than observed, and a decode wrong by a factor
    is the failure worth catching. The honest response to one is no field at all -- a
    dashboard showing nothing is recoverable, a dashboard showing 2.5 C for a warm battery is
    a wrong number nobody has reason to doubt.

    THE ALL-ZERO BLOCK IS THE CASE THIS FUNCTION EXISTS FOR, and bounds alone do not catch it.
    An unsupported register range, a BMS that has stopped answering, or a proxy padding a
    short reply all produce six zero words, which decode to 0.0 C in pack 0 -- inside every
    bound and indistinguishable from a freezing battery. The pack IDs are what break the tie,
    because they are 1-based (see PACK_ID_RANGE), and checking them costs nothing.

    `min > max` is in here for the same money: it cannot happen on a correct decode of a real
    reading, so it is a third, scale-independent way for a misread block to announce itself.

    The cell IDs are deliberately NOT checked. A pack is a physical box with a known count; a
    cell index within one is a number this site has never enumerated, so any bound on it would
    be invented rather than known, and inventing one risks silencing a working reading.
    """
    lo, hi = TEMP_PLAUSIBLE_C
    pack_lo, pack_hi = PACK_ID_RANGE
    return (lo <= temps["min_cell_temp_c"] <= hi
            and lo <= temps["max_cell_temp_c"] <= hi
            and temps["min_cell_temp_c"] <= temps["max_cell_temp_c"]
            and pack_lo <= temps["min_cell_temp_pack"] <= pack_hi
            and pack_lo <= temps["max_cell_temp_pack"] <= pack_hi)


def decode_fault_block(words: list[int]) -> dict:
    """A 24-word read of FAULT_BLOCK -> raw words hex-keyed, plus two derived counts.

    Every word is kept, unchanged and unnamed: no fault or warning BIT is documented anywhere
    this repo has found, so the raw words remain the only honest record of what was set.

    THE COUNTS ARE POPCOUNTS, NOT NONZERO-WORD COUNTS. Each 32-bit value here is a bitmap, so
    one word holding three set bits is three active faults, not one, and a nonzero-word count
    would report `1` for it -- undercounting exactly when the number matters most. Counting
    set bits needs no bit-level knowledge at all, only the block's confirmed shape, so it says
    "how many are active" without pretending to know which.

    FAULTS AND WARNINGS ARE COUNTED SEPARATELY, and not only because a warning is not a fault.
    warning6 was outside the block until 2026-09-02 (see FAULT_BLOCK's comment) and has
    therefore never been observed on this site: if it turns out to carry a normally-set bit,
    it pins `active_warning_count` above zero for good. Keeping the split means that failure
    mode cannot reach `active_fault_count`, which is the number the dashboard alarms on.
    """
    if len(words) != FAULT_BLOCK[1]:
        raise ValueError(f"expected {FAULT_BLOCK[1]} words, got {len(words)}")
    base = FAULT_BLOCK[0]
    fields = {f"fault_raw_{base + i:04x}": word for i, word in enumerate(words)}
    fields["active_fault_count"] = sum(w.bit_count() for w in words[:FAULT_WORDS])
    fields["active_warning_count"] = sum(w.bit_count() for w in words[FAULT_WORDS:])
    return fields


def decode_daily_battery_block(words: list[int]) -> dict:
    """An 11-word read of DAILY_BATTERY_BLOCK -> SoH and the three lifetime energy counters.

    Unlike the fault and firmware blocks, this one IS decoded into named fields: the addresses
    and the scale were both confirmed against the live inverter (see DAILY_BATTERY_BLOCK's
    comment), which is the bar this module sets for naming anything.

    The four words between SoH and the counters (0x011C-0x011F) are read as part of one
    round-trip and dropped. Publishing them raw would imply this repo knows they mean
    something; all it knows is that they were zero.
    """
    if len(words) != DAILY_BATTERY_BLOCK[1]:
        raise ValueError(f"expected {DAILY_BATTERY_BLOCK[1]} words, got {len(words)}")
    base = DAILY_BATTERY_BLOCK[0]
    def at(addr, n=1):
        i = addr - base
        return words[i:i + n]

    return {
        "soh_pct": round(decode(at(REG_SOH)) * SOH_STEP_PCT, 1),
        "lifetime_charge_kwh":
            round(decode(at(REG_LIFETIME_CHARGE, 2)) * ENERGY_STEP_KWH, 1),
        "lifetime_discharge_kwh":
            round(decode(at(REG_LIFETIME_DISCHARGE, 2)) * ENERGY_STEP_KWH, 1),
        "lifetime_grid_charge_kwh":
            round(decode(at(REG_LIFETIME_GRID_CHARGE, 2)) * ENERGY_STEP_KWH, 1),
    }


def daily_battery_plausible(daily: dict) -> bool:
    """False when a decoded daily battery block cannot be describing this battery.

    Bounds, then two ORDERINGS, and the orderings are what make this more than a range check.
    Both are true by construction rather than by observation -- every kWh that came out of the
    battery went in first, and grid charging is a subset of all charging -- so neither can be
    violated by a correct decode of a real reading, at any scale. That is what makes them the
    check that survives being wrong about the units: the losing alignment in
    DAILY_BATTERY_BLOCK's comment fails them outright, because it splices adjacent counters
    together and destroys the relationship between them.

    ROUND-TRIP IS NOT CHECKED, deliberately. The confirmed read gives 1022.1/1048.1 = 97.5%,
    above the 90-96% an AC round trip would show -- consistent with these being DC-side
    counters, which is the same distinction that makes the collector's `load_power_w` a derived
    figure. A tighter bound here would encode that inference as a fact and start silencing a
    working register on the strength of it.
    """
    lo, hi = LIFETIME_ENERGY_PLAUSIBLE_KWH
    soh_lo, soh_hi = SOH_PLAUSIBLE_PCT
    charge = daily["lifetime_charge_kwh"]
    return (soh_lo <= daily["soh_pct"] <= soh_hi
            and lo <= charge <= hi
            and lo <= daily["lifetime_discharge_kwh"] <= hi
            and lo <= daily["lifetime_grid_charge_kwh"] <= hi
            and daily["lifetime_discharge_kwh"] <= charge
            and daily["lifetime_grid_charge_kwh"] <= charge)


def decode_daily_inverter_block(words: list[int]) -> dict:
    """A 1-word read of DAILY_INVERTER_BLOCK -> the inverter's heatsink temperature.

    SIGNED, for the same reason the cell temperatures are: read unsigned, a heatsink at -0.1 C
    publishes as +6553.5 C. A cold start on a winter morning is the case, and it is a real one
    here in a way it is not for a battery pack that lives indoors.
    """
    if len(words) != DAILY_INVERTER_BLOCK[1]:
        raise ValueError(f"expected {DAILY_INVERTER_BLOCK[1]} words, got {len(words)}")
    return {"inverter_temp_c": round(decode(words, signed=True) * 0.1, 1)}


def inverter_temp_plausible(daily: dict) -> bool:
    """False when a decoded heatsink temperature cannot be describing this inverter.

    One value and no cross-check, so this is the weakest guard in the module -- see
    INVERTER_TEMP_PLAUSIBLE_C for what the lower bound gives up in exchange for catching an
    all-zero block.
    """
    lo, hi = INVERTER_TEMP_PLAUSIBLE_C
    return lo <= daily["inverter_temp_c"] <= hi


def decode_daily_pv_block(words: list[int]) -> dict:
    """A 2-word read of DAILY_PV_BLOCK -> lifetime PV energy. See DAILY_PV_BLOCK's comment for
    why the address rests on the live read alone."""
    if len(words) != DAILY_PV_BLOCK[1]:
        raise ValueError(f"expected {DAILY_PV_BLOCK[1]} words, got {len(words)}")
    return {"lifetime_pv_kwh": round(decode(words) * ENERGY_STEP_KWH, 1)}


def lifetime_pv_plausible(daily: dict) -> bool:
    """False when a decoded lifetime PV total cannot be describing this array.

    Same zero-rejecting bound as the battery counters, and it carries more weight here: this
    address appears in no document at all, so an unsupported-register zero is a likelier
    failure for this field than for any other in the tier.
    """
    lo, hi = LIFETIME_ENERGY_PLAUSIBLE_KWH
    return lo <= daily["lifetime_pv_kwh"] <= hi


def decode_firmware_block(words: list[int]) -> dict:
    """A 6-word read of FIRMWARE_BLOCK -> raw words, hex-keyed. See FIRMWARE_BLOCK's comment:
    which word is which named field is not confirmed, so nothing here is decoded."""
    if len(words) != FIRMWARE_BLOCK[1]:
        raise ValueError(f"expected {FIRMWARE_BLOCK[1]} words, got {len(words)}")
    base = FIRMWARE_BLOCK[0]
    return {f"firmware_raw_{base + i:04x}": word for i, word in enumerate(words)}


def decode_inverter_fw_block(words: list[int]) -> dict:
    """A 20-word read of INVERTER_FW_BLOCK -> raw words, hex-keyed. See INVERTER_FW_BLOCK's
    comment."""
    if len(words) != INVERTER_FW_BLOCK[1]:
        raise ValueError(f"expected {INVERTER_FW_BLOCK[1]} words, got {len(words)}")
    base = INVERTER_FW_BLOCK[0]
    return {f"inverter_fw_raw_{base + i:04x}": word for i, word in enumerate(words)}


def decode_system_config_block(words: list[int]) -> dict:
    """A 16-word read of SYSTEM_CONFIG_BLOCK -> raw words, hex-keyed. See SYSTEM_CONFIG_BLOCK's
    comment."""
    if len(words) != SYSTEM_CONFIG_BLOCK[1]:
        raise ValueError(f"expected {SYSTEM_CONFIG_BLOCK[1]} words, got {len(words)}")
    base = SYSTEM_CONFIG_BLOCK[0]
    return {f"system_config_raw_{base + i:04x}": word for i, word in enumerate(words)}


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
