"""Publishing the slots themselves, so something other than the dispatcher can see them.

`slots.json` is the control path and it lives on a named volume inside the container. Nothing
else can read it: not Grafana, which speaks only InfluxDB, and not you, at the moment it
matters most -- when the container is down is exactly when "what would it be doing right now"
becomes worth asking, and that is precisely when the file is unreachable.

The gap that leaves is not theoretical. `grafana/generate-battery-plan.py` answers "what
dispatch would do" by REIMPLEMENTING `translator.classify()` in Flux, thresholds and all: a
second copy of the control logic, in a second language, which nothing tests for agreement and
which cannot see the runtime surplus release at all. Publishing the real slots retires the
need for it and gives the dashboard pre-merged blocks instead of 144 fifteen-minute rows.

WHY THIS IS NOT `state.StatePublisher`. Different measurement, different tags, a batch write
rather than one point, and a different process on a different cadence. What the two do share
is the failure posture, and that is stated once in each: best-effort, never raising, logged
once per transition. `slots.json` is the thing the dispatcher acts on; this is a copy for
looking at, and a copy that could break the translator would be worth less than no copy.

Structured like `state.py` for the same reason: `build_records` is pure and carries every
decision worth testing, `SlotPublisher` owns the client and is deliberately dull.
"""
from __future__ import annotations

import datetime as dt
import logging

log = logging.getLogger("dispatch")

MEASUREMENT = "dispatch_slots"


def _instant(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def build_records(doc: dict, sys_sn: str = "") -> list[dict]:
    """One record per slot: `{"time", "tags", "fields"}`. Pure.

    TIMESTAMPED AT THE SLOT'S START, IN THE FUTURE. These are points about time that has not
    happened yet, exactly like the `plan` measurement they derive from, which is why every
    dashboard query against that bucket carries a `stop:` in the future. A reader who forgets
    that gets an empty table and no error.

    TAGGED WITH `plan_run`, which is what makes re-publishing safe. The translator now runs
    every five minutes (#107) while the planner runs hourly, so most runs re-translate a plan
    that has not changed: same tag, same timestamps, same values, and Influx overwrites in
    place rather than accumulating twelve copies an hour. Series growth follows the PLANNER's
    cadence, which is the shape the `planning` bucket has carried since July.

    It is also what lets a query pick one run's slots and not a mixture. Slot boundaries move
    between runs, so an untagged write would leave yesterday's slots stranded at timestamps
    today's run does not cover -- visible, plausible, and wrong. The `NEWEST` snippet the
    other dashboards already use handles this, provided the tag is here to filter on.

    `end_s` is a FIELD and not a second point. A slot is one thing with two edges, and
    splitting it into a start and an end row would make every consumer re-pair them.
    """
    tags = {"plan_run": doc.get("plan_run", "")}
    if sys_sn:
        tags["sys_sn"] = sys_sn

    records = []
    for slot in doc.get("slots", []):
        start, end = _instant(slot["start"]), _instant(slot["end"])
        fields: dict[str, int | float | str] = {
            "action": str(slot["action"]),
            "end_s": int(end.timestamp()),
            "duration_s": int((end - start).total_seconds()),
        }
        # Present only for charge and discharge, mirroring `Slot.__post_init__`, which makes
        # a `self` or `hold` slot carrying either of these an error rather than a curiosity.
        # Publishing zeros instead would read as "charge at 0 W to 0 %", which is a command
        # this dispatcher can issue and therefore a genuinely ambiguous thing to write down.
        if slot.get("power_w") is not None:
            fields["power_w"] = int(slot["power_w"])
            fields["target_soc"] = float(slot["target_soc"])
        records.append({"time": start, "tags": dict(tags), "fields": fields})
    return records


class SlotPublisher:
    """Writes `dispatch_slots` to the `alphaess` bucket, and never raises."""

    def __init__(self, write_api, bucket: str, sys_sn: str = ""):
        self.write_api = write_api
        self.bucket = bucket
        self.sys_sn = sys_sn
        self._failing = False

    def publish(self, doc: dict) -> bool:
        """Returns whether the write succeeded. The translator logs it and carries on."""
        if self.write_api is None:
            return False
        from influxdb_client import Point

        points = []
        for rec in build_records(doc, self.sys_sn):
            point = Point(MEASUREMENT)
            for key, value in rec["tags"].items():
                point = point.tag(key, value)
            for key, value in rec["fields"].items():
                point = point.field(key, value)
            points.append(point.time(rec["time"]))
        if not points:
            return False

        try:
            # One call for the whole horizon. Fifty synchronous round trips would make a
            # failure partial, and a partly-written horizon is worse than an unwritten one:
            # it renders as a plan with a hole in it rather than as a plan that is missing.
            self.write_api.write(bucket=self.bucket, record=points)
        except Exception as exc:
            if not self._failing:
                log.warning("dispatch_slots write failed (further failures quiet): %s", exc)
                self._failing = True
            return False
        if self._failing:
            log.info("dispatch_slots writes recovered")
            self._failing = False
        return True
