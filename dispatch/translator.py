"""Plan intervals -> dispatch slots. DESIGN-dispatch.md section 4.

Pure: no I/O, no clock, no Modbus. Everything is a function of the intervals passed in, so
the golden tests and the review charts run the same code the dispatcher does.

The central judgement here is section 4.1's, and it is worth restating because it looks like
an omission rather than a decision:

    Where the plan is SPECIFIC about wattage -- sell 4 kW into the evening peak, buy at the
    03:00 trough -- forced dispatch is right. Where the plan is INDIFFERENT to the exact
    wattage -- cover the house load, absorb whatever surplus exists -- the right command is
    NO command. Plain self-consumption already does that, and it tracks reality instead of a
    forecast.

Commanding a forced charge at `charge_wh x 4` W on a PV-surplus interval would pull the
shortfall from the grid whenever solar underdelivers -- importing energy the plan explicitly
priced at zero. Solar underdelivering against forecast is the normal case, not an edge case.

Consequence, named here because it is load-bearing elsewhere: `self` is emitted as a real
decision, and at the register level `start=0` is indistinguishable from a crashed dispatcher.
Nothing on the wire can tell those apart, which is why the heartbeat and the Kuma monitors in
section 6 are not a convenience.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from plan import PlanFormatError, PlanInterval, interval_minutes
from plan import iso_z as _iso

# Below this, an interval's energy is a rounding artefact rather than an intention.
# `advise.py` uses 1 Wh, which is right for a human-readable report and far too low here: at
# 15-minute intervals 1 Wh is 4 W of dispatch, and a slot exists to make the inverter do
# something. 50 Wh is 200 W at quarter-hour cadence -- small, but unambiguously deliberate.
ENERGY_FLOOR_WH = 50.0

# Surplus large enough to be worth harvesting -- see `_can_harvest`. Deliberately NOT
# `ENERGY_FLOOR_WH`, which answers a different question: that floor asks "did the LP intend a
# dispatch", and 50 Wh is the right bar for making the inverter do something. This one asks
# "is there any solar going to the meter", where the answer is worth having at 40 W. Reusing
# the 50 Wh floor here was measured to drop three of the four harvestable intervals on
# 2026-08-05, whose exports sit at exactly 50 Wh -- right on the boundary.
SURPLUS_FLOOR_WH = 10.0

# How close to the plan's capacity counts as "the plan thinks the battery is full".
FULL_TOLERANCE_WH = 50.0

ACTIONS = ("charge", "discharge", "self", "hold")


@dataclass(frozen=True)
class Slot:
    """One dispatch instruction over a half-open interval [start, end).

    `power_w` is an unsigned magnitude; `action` carries the direction. That matches the
    slots.json contract in section 4.3 and the handover dispatcher, which derives its sign
    from the action. It deliberately does NOT match `registers.Command`, whose `power_w` is
    signed charging-positive -- the conversion happens at the dispatcher, once.
    """
    start: dt.datetime
    end: dt.datetime
    action: str
    power_w: int | None = None
    target_soc: float | None = None

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        needs_power = self.action in ("charge", "discharge")
        if needs_power and (self.power_w is None or self.target_soc is None):
            raise ValueError(f"{self.action} slot requires power_w and target_soc")
        if not needs_power and (self.power_w is not None or self.target_soc is not None):
            raise ValueError(f"{self.action} slot must not carry power_w/target_soc")
        if self.end <= self.start:
            raise ValueError(f"slot ends at or before it starts: {self.start} -> {self.end}")

    def as_json(self) -> dict:
        d = {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "action": self.action,
        }
        if self.power_w is not None:
            d["power_w"] = self.power_w
            d["target_soc"] = self.target_soc
        return d


def _can_harvest(
    iv: PlanInterval,
    capacity_wh: float | None,
    surplus_floor: float = SURPLUS_FLOOR_WH,
) -> bool:
    """Is this an interval where releasing dispatch harvests solar the plan cannot see?

    THE GAUGE SATURATES BEFORE THE BATTERY DOES. Measured over 25 days of collector data
    (2026-07-22..2026-08-15): once `soc_percent` reads 100 the battery goes on absorbing a
    mean of 1,375 Wh/day, max 2,240 Wh, on 16 of 25 days. It is real stored energy, not a
    reporting artefact -- 18,615 Wh of the 22,005 Wh absorbed came back out again before the
    gauge left 100 %, which is 85 %, i.e. ordinary round-trip loss. Every Wh of it was solar:
    grid-sourced charge during those 1,075 minutes was 0 Wh.

    THIS HEADROOM IS UNREACHABLE BY COMMAND. `target_soc_pct` in 0x0886 is a percentage OF
    THE GAUGE, so once the gauge reads 100 a Mode 2 command asking for 100 has nothing left
    to ask for -- the inverter believes it has arrived. The only control state that keeps
    absorbing past that point is self-consumption, where there is no target and the BMS takes
    PV until it physically stops. So on these intervals `self` is not marginally better than
    `hold`; it is the difference between banking ~1.4 kWh of free solar and exporting it.

    ONLY AT THE PLAN'S OWN FULL. Below capacity the LP had room and surplus and still chose
    not to charge -- that is a decision (keeping room for a cheaper trough later, or valuing
    the export), and overriding it would be second-guessing the optimiser inside its own
    model. At capacity the LP is not choosing; it has simply run out of the variable. That is
    the only place this rule is entitled to act.

    NO CALENDAR. The condition is read from the plan, and the season falls out of it: over
    the whole archive it fires in the 16:00 and 17:00 local hours only, on 15 of 16 days.
    A hardcoded June-August gate would idle through a dull July and miss a bright May.
    """
    if capacity_wh is None:
        return False
    if iv.soc_wh < capacity_wh - FULL_TOLERANCE_WH:
        return False
    if iv.import_wh > surplus_floor:
        # The plan forecasts a deficit. Releasing here spends the inventory the plan is
        # holding -- on 2026-08-05 that is the three intervals before a 1,175 Wh/interval
        # dump into the evening peak. Freeze, whatever the sun is doing.
        return False
    if iv.export_wh > surplus_floor:
        return True
    # Plan-BALANCED: the LP forecasts PV exactly meeting the house, so it has the battery
    # neither charging nor discharging. `hold` and `self` are then IDENTICAL IN THE PLAN'S
    # OWN MODEL, and releasing costs nothing the optimiser was counting on.
    #
    # It is worth releasing because the balance point is where the plan is least reliable.
    # `export_wh` is a DIFFERENCE OF TWO FORECASTS, so where they cancel its sign is set by
    # the error rather than the signal. Measured against actuals over the same 15-minute
    # windows, the PV forecast runs ~2.3x low in the late afternoon -- at 17:00 local a
    # median 194 Wh forecast against 449 Wh actual, low on 88 % of intervals -- while the
    # load forecast is accurate (187 planned against 194 actual). Actual net at 17:00 is a
    # median +285 Wh with only 17 % of intervals in deficit. So `hold` at the balance point
    # is a coin flip called by the least trustworthy digit in the plan.
    #
    # THE PV TEST IS WHAT MAKES THIS SAFE YEAR-ROUND. In a balanced interval export and
    # import are both zero, so `pv_forecast_wh` EQUALS the household load by identity. The
    # rule therefore fires only where the plan says solar is covering the whole house --
    # a "the sun is strong right now" test, which self-gates in winter without a calendar
    # and cannot fire at night. Across the archive those intervals carry 158-241 Wh per
    # quarter hour, i.e. 630-960 W of sun. Defensive rather than observed: no archived
    # interval was balanced with no sun, because a dark balanced interval needs a house
    # drawing nothing.
    return iv.pv_forecast_wh > surplus_floor


def classify(
    iv: PlanInterval,
    floor: float = ENERGY_FLOOR_WH,
    *,
    capacity_wh: float | None = None,
    surplus_floor: float = SURPLUS_FLOOR_WH,
) -> str:
    """One interval -> one action. Section 4.1's table, verbatim.

    The distinction that matters is not charge-vs-discharge, it is whether the energy crosses
    the meter. Discharging into the house and discharging to the grid are the same battery
    behaviour at different prices, and only the second needs commanding.

    `capacity_wh` is optional so this stays callable on an interval alone; without it the
    harvest rule is simply off, which is the pre-2026-08-15 behaviour.
    """
    charging = iv.charge_wh > floor
    discharging = iv.discharge_wh > floor

    if charging and discharging:
        # The LP should never emit both in one interval -- it would be paying round-trip
        # losses to stand still. If it does, something upstream is wrong and picking a
        # winner silently would hide it.
        raise PlanFormatError(
            f"interval {_iso(iv.start)} both charges ({iv.charge_wh:.0f} Wh) and "
            f"discharges ({iv.discharge_wh:.0f} Wh)")

    if discharging:
        return "discharge" if iv.export_wh > floor else "self"
    if charging:
        return "charge" if iv.import_wh > floor else "self"
    # Standing still. Freezing the battery is right whenever the plan is protecting its
    # inventory, and wrong on the one shape `_can_harvest` names.
    return "self" if _can_harvest(iv, capacity_wh, surplus_floor) else "hold"


def to_slots(
    intervals: list[PlanInterval],
    capacity_wh: float,
    floor: float = ENERGY_FLOOR_WH,
) -> tuple[list[Slot], list[str]]:
    """The section 4 algorithm. Returns (slots, warnings).

    Warnings are returned rather than logged so the caller decides where they go -- the
    dispatcher sends them to a Kuma monitor, the review harness prints them under the chart.
    """
    if len(intervals) < 2:
        raise PlanFormatError("need at least two intervals to translate")

    intervals = sorted(intervals, key=lambda i: i.start)
    minutes = interval_minutes(intervals)
    per_hour = 60.0 / minutes
    span = dt.timedelta(minutes=minutes)
    warnings: list[str] = []

    slots: list[Slot] = []
    for pos, iv in enumerate(intervals):
        action = classify(iv, floor, capacity_wh=capacity_wh)
        power_w = target_soc = None

        if action in ("charge", "discharge"):
            wh = iv.charge_wh if action == "charge" else iv.discharge_wh
            power_w = round(wh * per_hour)

            # Section 3.2: the target is THIS interval's own soc_wh, not the next point's.
            target_soc = round(100.0 * iv.soc_wh / capacity_wh, 1)

            # The direction rule: a charge command needs a target above where the battery
            # starts the interval, a discharge command below it. The plan's own trajectory
            # supplies "where it starts" -- the previous interval's END soc.
            #
            # Downgrade rather than emit, because a Mode 2 command whose target sits the
            # wrong side of the current SoC is a silent no-op: the inverter accepts it and
            # does nothing, and every monitor stays green.
            #
            # ONLY within a single plan run. After `newest_by_interval()` adjacent intervals
            # routinely come from different runs, and each run re-anchors to the battery's
            # ACTUAL SoC at its own plan time (`initialCharge`, read live from Influx). So the
            # previous run's projection for this instant is a stale forecast, not a
            # measurement, and comparing across the seam compares two trajectories.
            #
            # Found on the real archive: every one of the six warnings this produced fell
            # exactly on a 3-hourly run boundary -- e.g. 2026-08-13T21:00Z, where the outgoing
            # run projected 5803 Wh and the incoming run re-anchored at 7021 Wh. No
            # single-run fixture can reproduce this, which is why the corpus is real.
            prev = intervals[pos - 1] if pos else None
            start_soc = (
                round(100.0 * prev.soc_wh / capacity_wh, 1)
                if prev is not None and prev.plan_run == iv.plan_run
                else None
            )
            if start_soc is not None:
                # Downgrades land on `hold`, never on the harvest release. A downgrade is a
                # FAULT path -- it fires with a warning because the plan asked for something
                # the register cannot express -- and freezing is the conservative answer when
                # the plan has already stopped making sense. The harvest rule also cannot
                # reach here in the charge case anyway: a `charge` command requires
                # `import_wh > ENERGY_FLOOR_WH`, which contradicts `_can_harvest`'s
                # near-zero-import test by construction.
                downgrade = "hold"
                if action == "charge" and target_soc <= start_soc:
                    warnings.append(
                        f"{_iso(iv.start)}: charge target {target_soc}% is not above the "
                        f"plan's own start-of-interval SoC {start_soc}% "
                        f"-- downgraded to {downgrade}")
                    action, power_w, target_soc = downgrade, None, None
                elif action == "discharge" and target_soc >= start_soc:
                    warnings.append(
                        f"{_iso(iv.start)}: discharge target {target_soc}% is not below the "
                        f"plan's own start-of-interval SoC {start_soc}% "
                        f"-- downgraded to {downgrade}")
                    action, power_w, target_soc = downgrade, None, None

            if power_w is not None and power_w <= 0:
                downgrade = "hold"
                warnings.append(
                    f"{_iso(iv.start)}: {action} of {wh:.0f} Wh rounds to {power_w} W "
                    f"-- downgraded to {downgrade}")
                action, power_w, target_soc = downgrade, None, None

        slots.append(Slot(iv.start, iv.start + span, action, power_w, target_soc))

    return _merge(slots), warnings


def _merge(slots: list[Slot]) -> list[Slot]:
    """Merge adjacent slots ONLY when action, power and target are all identical.

    This is a deliberate departure from `advise.py`, which collapses intervals into blocks
    because its reader is a human who does not want 104 rows. The dispatcher rewrites the
    register every 60 s regardless, so merging buys nothing operationally and costs the plan's
    per-interval power shaping. In practice this merges long runs of `self` and `hold` and
    almost nothing else, because `target_soc` moves every interval while charging -- which is
    exactly the intent. Let the file be long.
    """
    out: list[Slot] = []
    for s in slots:
        prev = out[-1] if out else None
        if (prev and prev.end == s.start and prev.action == s.action
                and prev.power_w == s.power_w and prev.target_soc == s.target_soc):
            out[-1] = Slot(prev.start, s.end, s.action, s.power_w, s.target_soc)
        else:
            out.append(s)
    return out


def build_document(
    intervals: list[PlanInterval],
    capacity_wh: float,
    generated_at: dt.datetime,
    floor: float = ENERGY_FLOOR_WH,
) -> tuple[dict, list[str]]:
    """The full slots.json payload. Section 4.3's contract.

    `generated_at` is injected rather than read from the clock so the goldens are stable and
    the whole module stays a pure function of its inputs.
    """
    slots, warnings = to_slots(intervals, capacity_wh, floor)
    runs = {iv.plan_run for iv in intervals if iv.plan_run}
    doc = {
        "generated_at": _iso(generated_at),
        # Plural because section 3.3 allows a newer short-horizon run to sit in front of an
        # older one covering the tail. Usually one entry.
        "plan_run": sorted(runs)[-1] if runs else "",
        "plan_runs": sorted(runs),
        "horizon_end": _iso(slots[-1].end),
        "interval_minutes": interval_minutes(sorted(intervals, key=lambda i: i.start)),
        "capacity_wh": capacity_wh,
        "slots": [s.as_json() for s in slots],
    }
    return doc, warnings
