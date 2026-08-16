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
) -> dict:
    """One `dispatch_state` point's fields. Pure -- every value is a function of the inputs.

    `now` is passed in rather than read from the clock so `expires_at` is reproducible.
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
