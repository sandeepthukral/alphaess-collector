"""Encodings for the dispatch block. DESIGN-dispatch.md section 5, `dispatch/registers.py`.

This is the layer that fails silently: a bad encoding does not raise, it writes a plausible
number to a real inverter and the battery does something unintended for fifteen minutes. So
the tests here are mostly round-trips and boundaries rather than examples.
"""
from __future__ import annotations

import pytest

import registers as R
from registers import Command, DispatchMode


class TestPowerEncoding:
    """`power_w` is charging-positive here and discharge-positive on the wire, offset by
    +32000. Two sign conventions and an offset in one 32-bit value."""

    @pytest.mark.parametrize("watts", [0, 1, -1, 500, -500, 4850, -4700, 32000, -32000])
    def test_round_trips(self, watts):
        assert R.decode_power(R.encode_power(watts)) == watts

    def test_zero_is_the_offset(self):
        assert R.encode_power(0) == [0, R.POWER_OFFSET]

    def test_charging_is_below_the_offset(self):
        # Charging positive in our units -> the raw value goes DOWN. Getting this backwards
        # charges when the plan said discharge, which is the single most expensive bug here.
        assert R.decode(R.encode_power(4000)) == R.POWER_OFFSET - 4000

    def test_discharging_is_above_the_offset(self):
        assert R.decode(R.encode_power(-4500)) == R.POWER_OFFSET + 4500

    def test_discharge_above_767_w_stays_unsigned(self):
        """The reference CSV marks 0x0881 `signed`, which is wrong.

        Reading it signed works by accident below 32767 and breaks above -- i.e. for any
        discharge over 767 W, which is most real commands. This pins the unsigned reading.
        """
        raw = R.decode(R.encode_power(-1000))
        assert raw == 33000
        assert R.decode(R.encode_power(-1000), signed=True) == 33000  # still positive, 16-bit

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            R.encode_power(-(2**32))


class TestSocEncoding:
    """0.4 %/bit, not the community's 0.392 -- see registers.py for the discriminating
    evidence (the app's 100 % force-charge reading exactly 250)."""

    def test_the_force_charge_observation(self):
        assert R.encode_soc(100.0) == [250]
        assert R.decode_soc([250]) == 100.0

    @pytest.mark.parametrize("pct", [0.0, 0.4, 10.0, 20.0, 50.0, 78.0, 99.6, 100.0])
    def test_representable_values_round_trip(self, pct):
        assert R.decode_soc(R.encode_soc(pct)) == pct

    def test_unrepresentable_values_snap_to_the_step(self):
        # 78.1 does not exist at 0.4 %/bit. The lossiness is in the hardware; what matters is
        # that it snaps predictably rather than drifting.
        assert R.decode_soc(R.encode_soc(78.1)) == 78.0

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            R.encode_soc(-1.0)


class TestInt32:
    @pytest.mark.parametrize("v", [0, 1, 300, 65535, 65536, 2**31 - 1])
    def test_round_trips(self, v):
        assert R.decode(R.encode_int32(v)) == v

    def test_big_endian_word_order(self):
        assert R.encode_int32(0x00010002) == [0x0001, 0x0002]

    def test_decode_rejects_odd_widths(self):
        with pytest.raises(ValueError, match="1 or 2 registers"):
            R.decode([1, 2, 3])


class TestCommandValidation:
    def test_mode_2_requires_a_target(self):
        with pytest.raises(ValueError, match="requires a target"):
            Command(DispatchMode.SOC_TARGET, -4000, None, 300)

    def test_mode_3_hold_may_omit_the_target(self):
        cmd = Command(DispatchMode.FOLLOW, 0, None, 300)
        assert cmd.target_soc_pct is None

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown dispatch mode"):
            Command(7, 0, None, 300)

    def test_zero_duration_rejected(self):
        # Not "forever" -- a command that expires immediately. A silent no-op made loud.
        with pytest.raises(ValueError, match="must be positive"):
            Command(DispatchMode.FOLLOW, 0, None, 0)

    @pytest.mark.parametrize("pct", [-0.1, 100.1])
    def test_target_out_of_range_rejected(self, pct):
        with pytest.raises(ValueError, match="out of range"):
            Command(DispatchMode.SOC_TARGET, 0, pct, 300)


class TestEncodeCommand:
    def test_start_is_never_included(self):
        """The caller writes START last, so a partly-written command is never live."""
        cmd = Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300)
        assert R.REG_START not in R.encode_command(cmd)

    def test_soc_control_writes_every_payload_register(self):
        writes = R.encode_command(Command(DispatchMode.SOC_TARGET, -4500, 20.0, 300))
        assert writes == {
            R.REG_MODE: [2],
            R.REG_POWER: [0, 36500],      # 32000 + 4500
            R.REG_SOC: [50],              # 20.0 / 0.4
            R.REG_TIME: [0, 300],
        }

    def test_hold_omits_the_soc_register(self):
        """Section 5 step 6: a hold writes no SoC target.

        Mode 3 freezes the battery where it is, so a target is meaningless -- and writing one
        would leave a stale number for the next reader to misinterpret.
        """
        writes = R.encode_command(Command(DispatchMode.FOLLOW, 0, None, 300))
        assert R.REG_SOC not in writes
        assert writes[R.REG_MODE] == [3]
        assert writes[R.REG_POWER] == [0, R.POWER_OFFSET]

    def test_mode_register_is_0x0885_not_0x0883(self):
        """0x0883 is reactive power. The two were confused once, from memory."""
        assert R.REG_MODE == 0x0885
        assert R.REG_REACTIVE == 0x0883
        assert R.encode_command(
            Command(DispatchMode.FOLLOW, 0, None, 300))[0x0885] == [3]


class TestDecodeBlock:
    def _block(self, *, start=1, power_w=-4500, soc_pct=20.0, mode=2, duration=300):
        """Nine words as the inverter returns them, 0x0880-0x0888."""
        words = [0] * 9
        words[R.REG_START - R.REG_START] = start
        words[R.REG_POWER - R.REG_START:R.REG_POWER - R.REG_START + 2] = R.encode_power(power_w)
        words[R.REG_MODE - R.REG_START] = mode
        words[R.REG_SOC - R.REG_START] = R.encode_soc(soc_pct)[0]
        words[R.REG_TIME - R.REG_START:R.REG_TIME - R.REG_START + 2] = R.encode_int32(duration)
        return words

    def test_round_trips_a_command(self):
        state = R.decode_block(self._block())
        assert state["dispatch_active"] == 1
        assert state["power_w"] == -4500
        assert state["target_soc_pct"] == 20.0
        assert state["mode"] == 2
        assert state["mode_name"] == "SoC control"
        assert state["duration_s"] == 300

    def test_the_apps_force_charge_signature(self):
        """Caught on 2026-08-15 16:11: mode 2, +5000 W, 100 %, a 93-minute grid charge."""
        state = R.decode_block(
            self._block(power_w=5000, soc_pct=100.0, mode=2, duration=5580))
        assert state["power_w"] == 5000       # charging, positive in our convention
        assert state["target_soc_pct"] == 100.0
        assert state["duration_s"] == 5580

    def test_unknown_mode_is_named_not_hidden(self):
        assert "unknown" in R.decode_block(self._block(mode=9))["mode_name"]

    def test_wrong_word_count_raises(self):
        with pytest.raises(ValueError, match="expected 9 words"):
            R.decode_block([0] * 8)


class TestDescribe:
    def test_rows_carry_both_raw_and_meaning(self):
        """Half the value of the dashboard panel is checking a decode against the spec
        without leaving the dashboard -- the 0.392 error spread because nobody could see
        the raw value and the interpretation at once."""
        rows = R.describe(R.decode_block(
            [1, *R.encode_power(-4500), 0, 0, 2, 50, *R.encode_int32(300)]))
        by_addr = {addr: (name, raw, note) for addr, name, raw, note in rows}
        assert by_addr["0x0881"][1] == 36500
        assert "discharging at 4.5 kW" in by_addr["0x0881"][2]
        assert by_addr["0x0886"][1] == 50
        assert by_addr["0x0885"][2] == "SoC control"
        assert by_addr["0x0880"][2] == "Active"

    def test_hold_reads_as_hold_not_as_zero(self):
        rows = R.describe(R.decode_block(
            [1, *R.encode_power(0), 0, 0, 3, 0, *R.encode_int32(300)]))
        assert any("0 W (hold)" in note for *_, note in rows)
