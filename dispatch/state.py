"""Publishing what the inverter was told to do. DESIGN-dispatch.md section 7.1.

Kuma answers "is it broken". It cannot answer "what is the battery being told to do right
now, and why" -- a question asked from a phone, in the kitchen, watching the meter. The
dashboard has to answer that, and Grafana does not speak Modbus and must not: there is one
connection and this process is holding it. So the dispatcher is the only thing that can
publish these registers, and it does so from the readback it already performs.

No extra Modbus traffic. This is publishing a read that already happened.

DECODE AT WRITE TIME, NOT IN FLUX. Both the decoded fields and the raw block are stored: the
decoded ones are for reading, the raw ones are for the morning after, when a decode turns out
to have been wrong. At one point per minute keeping both costs nothing, and it is the only
way to re-derive history.

The split here mirrors the rest of `dispatch/`: `build_fields()` is pure and carries every
decision worth testing; `StatePublisher` owns the client and the failure handling, and is
deliberately dull.
"""
from __future__ import annotations

import datetime as dt
import logging

import registers as R

log = logging.getLogger("dispatch")

MEASUREMENT = "dispatch_state"

# Raw registers are published under their hex address, matching the spec PDFs and the
# dashboard's decode table. `raw_0880`..`raw_0888`.
RAW_PREFIX = "raw_"

# `reason` is free text built from exception strings and slot arithmetic, so it needs a cap.
# The same 200 the Kuma pings use (`scheduler.monitor_pings`), for the same reason: these two
# carry the same sentence to two places, and a reason that reads differently depending on
# where you saw it is worse than one that is merely long.
REASON_MAX = 200


def _decision_fields(
    decision_kind: str,
    reason: str,
    live: bool,
    live_soc_pct: float | None,
    write_verified: bool | None,
    actual_battery_w: float | None = None,
    voltages: dict | None = None,
    temps: dict | None = None,
    faults: dict | None = None,
    limits_hourly: tuple[int | None, int | None] | None = None,
    firmware: dict | None = None,
    inverter_fw: dict | None = None,
    system_config: dict | None = None,
    daily_battery: dict | None = None,
    daily_inverter: dict | None = None,
    daily_pv: dict | None = None,
) -> dict[str, int | float | str]:
    """What the DISPATCHER knows about this tick, as opposed to what the inverter said.

    Shared by both point shapes on purpose. Every value here is computed before a single
    register is touched, so none of it is lost when the inverter cannot be read -- and a
    Modbus outage is exactly when you want to know what the loop decided and why. That is the
    same argument `build_degraded_fields` already makes for `slot_action` and `plan_run`,
    applied to the four things that were previously only ever written to the log.

    `live` is published rather than inferred. `dispatch_active == 0` is what a dry run looks
    like and also what a healthy release looks like -- section 4.1 makes the release a
    decision like any other -- so a dashboard reading the register alone cannot tell "writing
    nothing because we are observing" from "writing nothing because the plan asked for it".

    `verified` is an int, not a bool, and that is deliberate. Grafana's stat panel reduces
    `""` by default, which means numeric fields only: a bool would be dropped exactly as a
    string is, forcing the `/.*/` path and value mappings where 0/1 gets thresholds -- the
    mechanism this repo already uses everywhere for red and green. `dispatch_active` sets the
    precedent, publishing a truth value as the int the register holds.
    """
    fields: dict[str, int | float | str] = {
        "decision_kind": str(decision_kind or "unknown"),
        # Unconditional, unlike everything else gated in this module, and the distinction is
        # the one the module turns on: the gated fields are ones that could not be READ, while
        # the loop always has a decision and always has a reason for it. `Decision.reason` is
        # never empty in production, so the fallback only covers a bare call.
        "reason": str(reason or "unspecified")[:REASON_MAX],
        "live": int(bool(live)),
    }
    # Both conditional, and for the same reason the rest of this module gates fields: the
    # honest report of a value that was not read is no field at all. `verified` has three
    # states and only two of them are a readback -- None means nothing was commanded this
    # tick, so there was nothing to confirm, which is the normal resting case and must not
    # render as a failure.
    if live_soc_pct is not None:
        fields["soc_pct"] = float(live_soc_pct)
    if write_verified is not None:
        fields["verified"] = int(bool(write_verified))
    # `registers.REG_BATTERY_POWER`, read in the SAME Modbus round-trip as the surplus check
    # (`scheduler.py` step 4) -- a separate register from the dispatch block, so this survives
    # exactly the failure `soc_pct` does. Charging-positive already, matching `setpoint_w`, so
    # the two can be compared on the same point without a sign flip or a cross-series join.
    if actual_battery_w is not None:
        fields["actual_battery_w"] = float(actual_battery_w)
    # `registers.VOLTAGE_BLOCK`, read alongside temps at the same point in the tick and gated
    # the same way -- absent means the block could not be read or decoded to a plausible
    # voltage. Same MIN/MAX-across-all-packs shape as temps, cell IDs decoded but not published.
    if voltages is not None:
        fields["min_cell_voltage_v"] = float(voltages["min_cell_voltage_v"])
        fields["min_cell_voltage_pack"] = int(voltages["min_cell_voltage_pack"])
        fields["max_cell_voltage_v"] = float(voltages["max_cell_voltage_v"])
        fields["max_cell_voltage_pack"] = int(voltages["max_cell_voltage_pack"])
    # `registers.TEMP_BLOCK`, the tick's LAST read -- taken after the write and the verify,
    # because nothing decides on a temperature and an observability read has no business
    # delaying a command (`scheduler.py` step 8b). Gated for the same
    # reason as the two above: absent means the block could not be read or decoded to a
    # plausible temperature, and there is no value that could stand in for that. Zero would
    # read as a freezing battery, and the last reading carried forward would hide a BMS that
    # has stopped answering -- both worse than a gap in the series.
    #
    # The MIN and MAX ACROSS ALL PACKS, tagged with the pack each came from. Not three
    # per-pack readings and not derivable into them; see `registers.TEMP_BLOCK`. The cell IDs
    # are decoded but not published -- the pack narrows it to a physical box, the cell within
    # it does not change what anyone does next.
    if temps is not None:
        fields["min_cell_temp_c"] = float(temps["min_cell_temp_c"])
        fields["min_cell_temp_pack"] = int(temps["min_cell_temp_pack"])
        fields["max_cell_temp_c"] = float(temps["max_cell_temp_c"])
        fields["max_cell_temp_pack"] = int(temps["max_cell_temp_pack"])
    # `registers.FAULT_BLOCK`, hourly-gated (`scheduler.py` step 8c). No fault or warning BIT
    # is named -- none is documented -- so every word goes out as-is, hex-keyed. What
    # `decode_fault_block` adds on top is two popcounts, `active_fault_count` and
    # `active_warning_count`: how many bits are set, which needs no bit-level knowledge, only
    # the block's confirmed shape. They arrive in the same dict as the raw words and are
    # published by the same update; the dashboard's "Active faults" tile reads the first.
    if faults is not None:
        fields.update({k: int(v) for k, v in faults.items()})
    # The inverter's own charge/discharge ceiling, already read hourly by `run()`'s
    # LIMITS_REFRESH_S for the clamp in `slots.clamp` -- republished here under the health
    # dashboard's field names rather than re-read, so this is the one field in this function
    # that can be present without a register having been touched THIS tick. Each half is
    # independently absent when `Inverter.limits()` itself could not read it.
    if limits_hourly is not None:
        max_charge, max_discharge = limits_hourly
        if max_charge is not None:
            fields["max_charge_power_w"] = int(max_charge)
        if max_discharge is not None:
            fields["max_discharge_power_w"] = int(max_discharge)
    # `registers.FIRMWARE_BLOCK`/`INVERTER_FW_BLOCK`/`SYSTEM_CONFIG_BLOCK`, weekly-gated
    # (`scheduler.py` step 8d) -- a tripwire, not a trend. Same raw-hex treatment as `faults`
    # and for the same reason: which word holds which named value is not confirmed anywhere in
    # this repo, so nothing here is decoded into a named field yet.
    if firmware is not None:
        fields.update({k: int(v) for k, v in firmware.items()})
    if inverter_fw is not None:
        fields.update({k: int(v) for k, v in inverter_fw.items()})
    if system_config is not None:
        fields.update({k: int(v) for k, v in system_config.items()})
    # `registers.DAILY_BATTERY_BLOCK`/`DAILY_INVERTER_BLOCK`/`DAILY_PV_BLOCK`, daily-gated
    # (`scheduler.py` step 8e). DECODED INTO NAMED FIELDS, unlike the raw-hex blocks above,
    # and the difference is evidence rather than effort: every address and both scales here
    # were confirmed against the live inverter on 2026-09-03, which is the bar `registers.py`
    # sets before it will name anything.
    #
    # Floats, including SoH. A stat panel reduces `""` to numeric fields (see the `verified`
    # note above), and while SoH only ever arrives on a 0.1 boundary, publishing it as an int
    # would silently floor 99.5 to 99 -- a degradation figure rounded the wrong way by the
    # storage layer is the kind of quiet error this whole tier exists to avoid.
    #
    # Three separate dicts, kept separate all the way here, because they are three independent
    # reads: the battery block being unreadable says nothing about the heatsink, and the
    # dashboard must be able to show one while the other is blank.
    for daily in (daily_battery, daily_inverter, daily_pv):
        if daily is not None:
            fields.update({k: float(v) for k, v in daily.items()})
    return fields


def describe_action(state: dict, decision_kind: str = "") -> str:
    """A phrase a human can read without knowing the register map.

    This is the field the dashboard leads with, so it says what is HAPPENING rather than
    which mode is set: "charging from grid" is the answer to the question being asked;
    "mode 2" is a fact about the protocol.
    """
    if not state.get("dispatch_active"):
        # `start=0` is a legitimate commanded state (section 4.1's `self`), and at the
        # register level it is indistinguishable from a crashed dispatcher. The distinction
        # comes from the decision, which only this process knows -- which is exactly why it
        # is published rather than inferred from the registers in Flux.
        return "self-consumption (released)" if decision_kind == "release" else "no dispatch"

    power = state.get("power_w", 0)
    mode = state.get("mode")
    if mode == R.DispatchMode.FOLLOW and power == 0:
        return "hold (battery frozen)"
    if power > 0:
        return "charging from grid" if mode == R.DispatchMode.SOC_TARGET else "charging from PV"
    if power < 0:
        return "discharging to grid"
    # A live block at 0 W in a mode that is not the verified hold. This dispatcher never
    # commands it -- it holds with Mode 3 -- so seeing it means the app is driving.
    #
    # Returned as ONE fixed string rather than interpolating the mode name, because the set
    # of values this function can produce is a contract: the dashboard colours it with value
    # mappings, and any string without a mapping renders in the base colour, which is the red
    # reserved for NO DISPATCHER. An unbounded set here would eventually paint a working
    # dispatcher as a failure. tests/test_dispatch_dashboard.py pins the two sides together.
    return "active at 0 W"


def build_fields(
    state: dict,
    raw_words: list[int],
    now: dt.datetime,
    decision_kind: str = "",
    slot: dict | None = None,
    plan_run: str = "",
    reason: str = "",
    live: bool = False,
    live_soc_pct: float | None = None,
    write_verified: bool | None = None,
    actual_battery_w: float | None = None,
    voltages: dict | None = None,
    temps: dict | None = None,
    faults: dict | None = None,
    limits_hourly: tuple[int | None, int | None] | None = None,
    firmware: dict | None = None,
    inverter_fw: dict | None = None,
    system_config: dict | None = None,
    daily_battery: dict | None = None,
    daily_inverter: dict | None = None,
    daily_pv: dict | None = None,
) -> dict:
    """One `dispatch_state` point's fields. Pure -- every value is a function of the inputs.

    `now` is passed in rather than read from the clock so `expires_at` is reproducible.

    `write_verified` rather than `verified`, because `scheduler.tick` already binds
    `verified` to the DECODED BLOCK. Two different things one letter apart, in the one place
    that handles both; the field on the point is `verified`, matching the log line an
    operator reads.
    """
    if len(raw_words) != R.DISPATCH_BLOCK[1]:
        raise ValueError(
            f"expected {R.DISPATCH_BLOCK[1]} raw words, got {len(raw_words)}")

    fields: dict[str, int | float | str] = {
        "dispatch_active": int(state["dispatch_active"]),
        "mode": int(state["mode"]),
        "mode_name": str(state["mode_name"]),
        "action": describe_action(state, decision_kind),
        # Already flipped into the dashboard's charging-positive convention by
        # `registers.decode_power` -- panel 9 negates `battery_power_w` in Flux to achieve
        # the same thing, and a "Commanded" stat disagreeing in sign with the "Battery Power"
        # stat beside it would be worse than no panel at all.
        "setpoint_w": int(state["power_w"]),
        "target_soc_pct": float(state["target_soc_pct"]),
        "duration_s": int(state["duration_s"]),
        **_decision_fields(decision_kind, reason, live, live_soc_pct, write_verified,
                          actual_battery_w, voltages, temps, faults, limits_hourly, firmware,
                          inverter_fw, system_config, daily_battery, daily_inverter,
                          daily_pv),
    }

    # `expires_at` is when the dead man's switch runs out if nothing is written again. It is
    # derived from write time plus duration rather than from the countdown register, because
    # section 5.1 records that register counting down erratically -- observed reading 300 s
    # three times across two minutes, then straight to expiry.
    if state["dispatch_active"]:
        fields["expires_at"] = int(now.timestamp()) + int(state["duration_s"])

    # These two are what make the panel diagnostic rather than decorative: without them the
    # dashboard shows a command, with them it shows WHICH PLAN asked for it -- the difference
    # between "the battery is charging at 15 ct" and "the battery is charging at 15 ct
    # because it is still serving a plan from six hours ago".
    if slot:
        fields["slot_start"] = int(
            dt.datetime.fromisoformat(slot["start"].replace("Z", "+00:00")).timestamp())
        fields["slot_action"] = str(slot["action"])
    if plan_run:
        fields["plan_run"] = plan_run

    base = R.DISPATCH_BLOCK[0]
    for offset, word in enumerate(raw_words):
        fields[f"{RAW_PREFIX}{base + offset:04x}"] = int(word)
    return fields


def build_degraded_fields(
    slot: dict | None = None,
    plan_run: str = "",
    read_error: str = "",
    decision_kind: str = "",
    reason: str = "",
    live: bool = False,
    live_soc_pct: float | None = None,
    write_verified: bool | None = None,
    actual_battery_w: float | None = None,
    voltages: dict | None = None,
    temps: dict | None = None,
    faults: dict | None = None,
    limits_hourly: tuple[int | None, int | None] | None = None,
    firmware: dict | None = None,
    inverter_fw: dict | None = None,
    system_config: dict | None = None,
    daily_battery: dict | None = None,
    daily_inverter: dict | None = None,
    daily_pv: dict | None = None,
) -> dict:
    """One `dispatch_state` point for a tick that decided but could not read the inverter.

    WHAT IS DELIBERATELY ABSENT. No `action`, no `setpoint_w`, no `dispatch_active`, no raw
    words. Those describe the hardware, and the hardware is what could not be read; the
    honest report of an unknown is no field, not a stale value carried forward and not a
    zero. Grafana's `last()` therefore keeps showing the previous readback, which is also the
    truth: a command already written stays live for its full `duration_s` whether or not this
    tick managed to look at it.

    WHAT IS DELIBERATELY PRESENT. `slot_action` and `plan_run` -- the decision, which this
    process knows perfectly well and which no failed read can take away. They are also two of
    the five fields `review-dry-run.py` pivots on, so publishing them is what stops a Modbus
    outage from rendering as a hole in the tick stream, indistinguishable from a loop that
    died. The `read_error` field is the reason, in the words the exception used.

    `_decision_fields` is present for the same reason, and `soc_pct` is the one worth
    pointing at: the SoC register and the dispatch block are two separate reads, and the
    common failure is the second one alone. So a degraded point routinely knows the live SoC
    the decision was made against, and dropping it because some other read failed would throw
    away a value that was successfully obtained. `actual_battery_w` is a third, separate read
    for the same reason -- the dispatch block can fail while the battery-power register still
    answers -- and the cell temperatures are a fourth. The health-poller fields (faults,
    limits, firmware, system config) are independent reads too, on their own gates, so a
    failed dispatch-block read has no bearing on whether they made it onto this point.

    Not merged into `build_fields()` with optional arguments: that function's contract is
    "every value is a function of a readback", and a version of it that sometimes has no
    readback would need a branch at every field. Two small functions, one of which cannot
    silently publish a half-truth.
    """
    fields: dict[str, int | float | str] = {
        "read_error": read_error or "inverter unreadable",
        **_decision_fields(decision_kind, reason, live, live_soc_pct, write_verified,
                          actual_battery_w, voltages, temps, faults, limits_hourly, firmware,
                          inverter_fw, system_config, daily_battery, daily_inverter,
                          daily_pv),
    }
    if slot:
        fields["slot_start"] = int(
            dt.datetime.fromisoformat(slot["start"].replace("Z", "+00:00")).timestamp())
        fields["slot_action"] = str(slot["action"])
    if plan_run:
        fields["plan_run"] = plan_run
    return fields


class StatePublisher:
    """Writes `dispatch_state` to the `alphaess` bucket, and never raises.

    Best-effort by construction: this is bookkeeping about the control loop, and it must not
    be able to stop it. A publish failure is logged once per transition rather than every
    tick -- at 60 s a broken InfluxDB would otherwise produce 1,440 identical lines a day and
    bury the thing that actually matters.
    """

    def __init__(self, write_api, bucket: str, sys_sn: str = ""):
        self.write_api = write_api
        self.bucket = bucket
        self.sys_sn = sys_sn
        self._failing = False

    def publish(self, fields: dict, now: dt.datetime | None = None) -> bool:
        """Returns whether the write succeeded. Callers may ignore it; the loop does."""
        if self.write_api is None:
            return False
        from influxdb_client import Point

        point = Point(MEASUREMENT)
        if self.sys_sn:
            point = point.tag("sys_sn", self.sys_sn)
        for key, value in fields.items():
            point = point.field(key, value)
        if now is not None:
            point = point.time(now)

        try:
            self.write_api.write(bucket=self.bucket, record=point)
        except Exception as exc:
            if not self._failing:
                log.warning("dispatch_state write failed (further failures quiet): %s", exc)
                self._failing = True
            return False
        if self._failing:
            log.info("dispatch_state writes recovered")
            self._failing = False
        return True
