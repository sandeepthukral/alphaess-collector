"""The planner's output, as this repo sees it.

`PlanInterval` is the only shape the translator understands. Two things produce it:

  - `from_influx()` -- the production path. Measurement `plan` in bucket `planning`.
  - `from_table()`  -- the planner's own stdout table, `plans/plan_YYYYMMDD_HH.txt`.

The second exists because the test corpus is in that format: `battery-planning`'s committed
golden files (`golden_summer_quarter_hour.txt`, `golden_winter_quarter_hour.txt`) are exactly
it, already reviewed, and every archived historical plan is too. Parsing them means the
translator can be tested and reviewed against real plan shapes without an InfluxDB, and
without committing household data -- a plan's `use` column is a load forecast, which is
occupancy data at 15-minute resolution, and this repo is public.

The two paths must agree. `from_table()` is not a convenience importer; it is the fixture
path for the golden tests, so a divergence between it and `from_influx()` would mean the
tests validate something the dispatcher never runs. The field mapping below is the contract,
and `tests/fixtures/planning_schema.json` (PLAN-repo-seams.md section 2b) is where it gets
pinned down.

TIME. The table prints LOCAL time -- `plan-now.sh` sets `BT_TZ`/`TZ` to Europe/Amsterdam --
while Influx stores UTC. Everything here normalises to aware UTC on the way in, because the
whole point of section 4.3's slot contract is that no local-clock string ever reaches the
control path. The October DST night has a genuinely ambiguous local hour; `fold=0` resolves it
to the first pass, which is the correct reading of a table printed in chronological order.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

PLANNER_TZ = ZoneInfo("Europe/Amsterdam")

# Column name in the planner's table -> attribute here. The planner prints more columns than
# the translator needs; anything not listed is deliberately dropped rather than carried along
# as dead weight.
TABLE_COLUMNS = {
    "chrg": "charge_wh",
    "dschg": "discharge_wh",
    "soc": "soc_wh",
    "imp": "import_wh",
    "exp": "export_wh",
    "pr-buy": "price_buy",
    "pr-sell": "price_sell",
    "cost": "cost_eur",
}

# Fields the translator cannot work without. Missing any of these is a hard failure that must
# reach monitor #2 with the field name attached, not a quiet zero -- see section 6.1.
REQUIRED_FIELDS = ("soc_wh", "charge_wh", "discharge_wh", "import_wh", "export_wh")


@dataclass(frozen=True)
class PlanInterval:
    """One planning interval.

    `start` is the interval's START, in aware UTC.

    `soc_wh` is the SoC at the interval's END -- it already includes this interval's own
    charge and discharge. That is not a convention chosen here; it falls out of the LP
    constraint at `Marstek-planning.py:2006-2008`:

        soc[t] == soc[t-1] + pvDirect[t] + Effcharge*charge[t] - discharge[t]/Effdischarge

    So the dispatch target for an interval is that interval's OWN soc_wh, never the next
    point's. Getting this backwards shifts every SoC target one interval late, which looks
    like a plausible plan that consistently underdelivers.

    `charge_wh` and `discharge_wh` are AC-side; `soc_wh` is battery-side, with the efficiency
    factors sitting between them. Convenient, and worth not undoing: the AC-side number is
    what you command as a power setpoint, the battery-side number is what you write to the
    SoC register. Do not derive either from the other.
    """
    start: dt.datetime          # aware UTC, interval start
    soc_wh: float               # END of interval, battery-side
    charge_wh: float            # AC-side
    discharge_wh: float         # AC-side
    import_wh: float
    export_wh: float
    plan_run: str = ""          # which planning run produced this; "" when unknown
    price_buy: float = 0.0
    price_sell: float = 0.0
    cost_eur: float = 0.0
    pv_forecast_wh: float = 0.0

    def __post_init__(self):
        if self.start.tzinfo is None:
            raise ValueError("PlanInterval.start must be timezone-aware")


class PlanFormatError(ValueError):
    """The plan could not be read. Carries the offending field or line so the message that
    reaches monitor #2 says what was wrong, per commit 78c94a9."""


def interval_minutes(intervals: list[PlanInterval]) -> int:
    """Infer the planning interval from consecutive starts.

    Inferred rather than configured because the planner switched from hourly to 15-minute
    when the NL day-ahead went to a 15-minute MTU on 2025-10-01, and the archive spans both.
    A fixture that is silently misread as the wrong cadence produces power setpoints wrong by
    4x, which is exactly the kind of error a golden file would freeze without comment.
    """
    if len(intervals) < 2:
        raise PlanFormatError("need at least two intervals to infer the cadence")
    deltas = {
        int((b.start - a.start).total_seconds() // 60)
        for a, b in zip(intervals, intervals[1:])
    }
    # A DST transition makes one gap 60 minutes longer or shorter in local time, but these
    # are UTC instants, so the set should be a single value.
    if len(deltas) != 1:
        raise PlanFormatError(f"inconsistent interval spacing: {sorted(deltas)} minutes")
    minutes = deltas.pop()
    if minutes not in (15, 30, 60):
        raise PlanFormatError(f"implausible interval of {minutes} minutes")
    return minutes


def from_table(text: str, plan_run: str = "") -> list[PlanInterval]:
    """Parse the planner's stdout table.

    Format, from `battery-planning/docs/PLAN.md` and its golden fixtures:

        date        time   pvD   pvI   use  nett chrgD  chrg dschg   soc   imp   exp  pr-buy ...
        2026-07-27 00:00     0     0    62    62     0     0     0 14000    62     0 +0.221106 ...

    Whitespace-delimited with a header row naming the columns, so the header is parsed rather
    than the positions assumed -- the column set has changed before and would otherwise
    misalign silently.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise PlanFormatError("empty plan")

    header = lines[0].split()
    if header[:2] != ["date", "time"]:
        raise PlanFormatError(f"unexpected header, wanted 'date time ...': {lines[0][:60]!r}")

    missing = [c for c in TABLE_COLUMNS if c not in header]
    if missing:
        raise PlanFormatError(f"plan table is missing column(s): {', '.join(missing)}")

    # pvD and pvI are summed into one forecast figure. pvD is DC-coupled PV and is always 0
    # on this site -- IDX_PV_DIRECT is commented "always 0 here - no such group" -- but it is
    # added rather than ignored so an archived plan from a different configuration still reads
    # correctly.
    pv_cols = [header.index(c) for c in ("pvD", "pvI") if c in header]
    idx = {col: header.index(col) for col in TABLE_COLUMNS}

    out: list[PlanInterval] = []
    prev_utc: dt.datetime | None = None
    for lineno, line in enumerate(lines[1:], start=2):
        parts = line.split()
        if len(parts) < len(header):
            raise PlanFormatError(f"line {lineno}: expected {len(header)} fields, got {len(parts)}")
        try:
            local = dt.datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M")
        except ValueError as e:
            raise PlanFormatError(f"line {lineno}: unparseable timestamp: {e}") from e

        values = {}
        for col, attr in TABLE_COLUMNS.items():
            raw = parts[idx[col]]
            try:
                values[attr] = float(raw)
            except ValueError as e:
                raise PlanFormatError(f"line {lineno}: column {col!r} is not a number: {raw!r}") from e

        # DST. On the autumn transition the local clock repeats 02:00-02:59, and the planner
        # prints the table in chronological order -- so those rows simply appear twice, with
        # identical local timestamps. `fold` is what distinguishes them, and it cannot be
        # inferred from a single row: both passes look the same in isolation.
        #
        # Resolved from the table's own ordering. fold=0 is the first pass; if that yields an
        # instant not strictly after the previous row, this must be the second pass, so
        # fold=1. Applying fold=0 unconditionally silently collapses the repeated hour onto
        # the first pass AND leaves the following UTC hour with no intervals at all -- on
        # 2026-10-25 that is 01:00-01:45Z with no slot, which the dispatcher would read as a
        # gap and fall back to self-consumption for an hour.
        start = local.replace(tzinfo=PLANNER_TZ, fold=0).astimezone(dt.UTC)
        if prev_utc is not None and start <= prev_utc:
            start = local.replace(tzinfo=PLANNER_TZ, fold=1).astimezone(dt.UTC)
            if start <= prev_utc:
                # Not a DST repeat -- genuinely out of order or duplicated. The translator
                # relies on the table being chronological, so this cannot pass quietly.
                raise PlanFormatError(
                    f"line {lineno}: timestamp {parts[0]} {parts[1]} is not after the "
                    f"previous row ({prev_utc.isoformat()}) under either DST fold")
        prev_utc = start

        out.append(PlanInterval(
            start=start,
            plan_run=plan_run,
            pv_forecast_wh=sum(float(parts[i]) for i in pv_cols),
            **values,
        ))

    if not out:
        raise PlanFormatError("plan table had a header but no rows")
    return out


PLAN_FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> pivot(rowKey:["_time","plan_run"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"])
"""


def from_influx(
    query_api,
    bucket: str,
    start: dt.datetime,
    stop: dt.datetime,
    measurement: str = "plan",
) -> list[PlanInterval]:
    """The production read. `query_api` is injected, matching `collector/pricing.py:409`.

    Returns EVERY matching point, including overlapping plan runs -- `newest_by_interval()`
    resolves those. Keeping the two steps separate means the overlap rule can be tested
    without a database.

    ONE DELIBERATE DEPARTURE from `pricing.py`: that module skips malformed records with a
    bare `except: continue`, which is right for a nightly cost backfill where a bad hour
    should not lose the day. Here it would be the wrong shape entirely -- a renamed field in
    `battery-planning` would silently yield zero intervals, the translator would write an
    empty slots file, and the dispatcher would go quiet with every monitor green. So a missing
    required field raises, and the caller sends the field name to monitor #2 with
    `status=down`, per PLAN-repo-seams.md section 2b.
    """
    flux = PLAN_FLUX.format(
        bucket=bucket, measurement=measurement,
        start=start.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        stop=stop.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
    )

    out: list[PlanInterval] = []
    seen_fields: set[str] = set()
    for table in query_api.query(flux):
        for rec in table.records:
            v = rec.values
            seen_fields.update(k for k in v if not k.startswith("_") and k != "result")
            missing = [f for f in REQUIRED_FIELDS if v.get(f) is None]
            if missing:
                raise PlanFormatError(
                    f"plan point at {rec.get_time()} is missing required field(s): "
                    f"{', '.join(missing)} -- the planner's schema has changed")
            out.append(PlanInterval(
                start=rec.get_time().astimezone(dt.UTC),
                plan_run=str(v.get("plan_run", "")),
                soc_wh=float(v["soc_wh"]),
                charge_wh=float(v["charge_wh"]),
                discharge_wh=float(v["discharge_wh"]),
                import_wh=float(v["import_wh"]),
                export_wh=float(v["export_wh"]),
                price_buy=float(v.get("price_buy") or 0.0),
                price_sell=float(v.get("price_sell") or 0.0),
                cost_eur=float(v.get("cost_eur") or 0.0),
                pv_forecast_wh=float(v.get("pv_forecast_wh") or 0.0),
            ))

    if not out:
        # An empty window is not obviously an error -- ask for a range before the planner ran
        # and you legitimately get nothing -- but on the control path it is always worth
        # saying so explicitly rather than returning [] into a translator that would then
        # write an empty slots file.
        raise PlanFormatError(
            f"no {measurement} points in bucket {bucket!r} between {start} and {stop}")
    return out


def iso_z(t: dt.datetime) -> str:
    """An instant as `...Z`, the format every artefact in this feature writes.

    One definition, because the alternative is what the archive already demonstrates: the
    planner's own tags mix `...Z` and `...+02:00`, and every reader of those has to parse
    rather than compare. Nothing this repo writes should add to that -- so the format lives
    beside `run_time`, which is the function that has to cope when it does not.
    """
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_time(tag: str) -> dt.datetime:
    """A `plan_run` tag as an instant.

    Module level rather than nested, because every consumer that orders runs needs exactly
    this and getting it wrong is silent: the planner writes UTC now, but points written
    before 2026-07-30 carry a local offset, and a string sort puts `...+02:00` before a `Z`
    tag from the same instant. `scripts/fetch-plan-corpus.py` picks the newest run with it.
    """
    if not tag:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    try:
        return dt.datetime.fromisoformat(tag.replace("Z", "+00:00"))
    except ValueError as e:
        raise PlanFormatError(f"unparseable plan_run {tag!r}: {e}") from e


def run_sort_key(tag: str):
    """Order `plan_run` tags by the INSTANT they name, not by their spelling.

    The one place this rule lives, because it has three callers and getting it wrong is
    silent. The planner writes UTC now, but tags written before 2026-07-30 carry a `+02:00`
    offset, and `"...17:26:14+02:00"` sorts after `"...16:00:00Z"` as a string while naming an
    instant half an hour earlier.

    Unparseable tags sort BEFORE every real one, ordered among themselves by string. That is
    not defensive padding: `from_table()` is documented as the fixture path and labels a plan
    with its filename -- `synthetic_dst_autumn` -- so the golden corpus legitimately carries
    tags that are not timestamps at all. They must not raise here, and they must not win
    "newest" over a real run either.
    """
    try:
        return (1, run_time(tag), "")
    except PlanFormatError:
        return (0, dt.datetime.min.replace(tzinfo=dt.UTC), tag)


def newest_by_interval(intervals: list[PlanInterval]) -> list[PlanInterval]:
    """Collapse overlapping plan runs: for each instant, keep the newest run covering it.

    Generalises `battery-planning/report_day.py:inForcePlans()`, which picks the most recent
    run at or before each interval. Looking forward every run is "before", so it reduces to
    newest-wins -- but the containment test still matters, because a newer run with a SHORTER
    horizon must fall back to an older run for the tail rather than leaving it unplanned.

    `plan_run` is compared as a parsed timestamp, not as a string. The planner writes UTC now,
    but points written before 2026-07-30 carry a local offset, and a string sort mixes the two
    formats wrongly -- the same trap `generate-battery-plan.py`'s NEWEST query documents.
    """
    # Every tag is parsed up front, not lazily inside the comparison. Comparing only on
    # collision would leave a corrupt `plan_run` completely unchecked whenever no two runs
    # happen to overlap at that instant -- and the translator's direction guard keys off
    # exactly this field, so an unreadable tag silently changes which intervals it trusts.
    times = {iv.plan_run: run_time(iv.plan_run) for iv in intervals}

    best: dict[dt.datetime, PlanInterval] = {}
    for iv in intervals:
        prev = best.get(iv.start)
        if prev is None or times[iv.plan_run] > times[prev.plan_run]:
            best[iv.start] = iv
    return [best[k] for k in sorted(best)]
