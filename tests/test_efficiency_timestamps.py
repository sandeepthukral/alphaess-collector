"""uploadTime is a naive local wall clock, and DST makes that ambiguous twice a year.

Getting this wrong does not produce an error, it produces a plausible number:
an hour of energy silently dropped in October, or an hour of records glued onto
the wrong instants in March. Both land in `conversion_loss_kwh` looking exactly
like a real loss, which is why the module also gates on SoC alignment -- but
this is the layer that has to be right first.
"""

import datetime as dt

import pytest

import efficiency
import pricing
from prices import NL_TZ

AUTUMN = dt.date(2026, 10, 25)  # 25 hours: 02:00-02:59 local happens twice
SPRING = dt.date(2026, 3, 29)   # 23 hours: 02:00-02:59 local does not exist


def rec(local_naive: dt.datetime, **fields) -> dict:
    return {"uploadTime": local_naive.strftime("%Y-%m-%d %H:%M:%S"), **fields}


def wall_clock_records(day: dt.date, count: int, step_min: int = 5) -> list[dict]:
    """What a device that just increments its wall clock emits.

    Naive local times, `count` of them, blind to DST. On a spring-forward day
    this claims an hour that never happened.
    """
    start = dt.datetime.combine(day, dt.time())
    return [rec(start + dt.timedelta(minutes=step_min * i)) for i in range(count)]


def real_instant_records(day: dt.date, count: int, step_min: int = 5) -> list[dict]:
    """What a device driven by a real clock emits: genuine instants, rendered
    as local wall time. On an autumn day the 02:00 hour appears twice."""
    start, _ = pricing.day_window_utc(day)
    return [
        rec((start + dt.timedelta(minutes=step_min * i)).astimezone(NL_TZ).replace(tzinfo=None))
        for i in range(count)
    ]


def test_a_plain_day_round_trips_every_record(summer_day):
    parsed = efficiency.parse_upload_times(wall_clock_records(summer_day, 288), summer_day)
    assert len(parsed) == 288
    instants = [t for t, _ in parsed]
    assert instants == sorted(instants)
    assert len(set(instants)) == 288
    start, end = pricing.day_window_utc(summer_day)
    assert start <= instants[0] and instants[-1] < end


def test_autumn_fold_keeps_both_passes_of_the_repeated_hour():
    """A 25-hour day holds 300 five-minute records, not 288.

    ZoneInfo resolves an ambiguous local time to fold=0 by default, so the
    second pass through 02:00-02:59 would map onto instants already used and
    collapse into them -- an hour of energy vanishing with nothing to show for
    it. The records arrive in order, so a step backwards is the tell.
    """
    parsed = efficiency.parse_upload_times(real_instant_records(AUTUMN, 300), AUTUMN)
    instants = [t for t, _ in parsed]
    assert len(instants) == 300
    assert len(set(instants)) == 300
    assert instants == sorted(instants)
    assert (instants[-1] - instants[0]).total_seconds() == pytest.approx(300 * 299)


def test_autumn_day_window_really_is_25_hours():
    """Pins the premise of the test above rather than trusting it."""
    start, end = pricing.day_window_utc(AUTUMN)
    assert (end - start) == dt.timedelta(hours=25)


def test_spring_gap_records_are_dropped_not_coerced():
    """02:00-02:59 local does not exist on 2026-03-29.

    Those wall-clock timestamps still convert to *some* instant, so nothing
    raises -- they would quietly land an hour away, on top of real records.
    Dropping them is the only honest option.
    """
    parsed = efficiency.parse_upload_times(wall_clock_records(SPRING, 288), SPRING)
    assert len(parsed) == 276
    locals_ = [t.astimezone(NL_TZ).hour for t, _ in parsed]
    assert 2 not in locals_
    start, end = pricing.day_window_utc(SPRING)
    assert (end - start) == dt.timedelta(hours=23)


def test_records_outside_the_local_day_are_clipped(summer_day):
    """AlphaESS has been seen returning boundary records from the next day."""
    records = wall_clock_records(summer_day, 288)
    records.append(rec(dt.datetime.combine(summer_day + dt.timedelta(days=1), dt.time())))
    records.insert(0, rec(dt.datetime.combine(summer_day, dt.time()) - dt.timedelta(minutes=5)))
    parsed = efficiency.parse_upload_times(records, summer_day)
    assert len(parsed) == 288


def test_duplicate_instants_collapse_with_the_last_one_winning(summer_day):
    """Duplicate rows are a documented AlphaESS data-quality episode. Two
    records on one instant must not be integrated as two 5-minute slots."""
    records = wall_clock_records(summer_day, 10)
    first = dict(records[3], load=111.0)
    second = dict(records[3], load=222.0)
    records[3] = first
    records.insert(4, second)
    parsed = efficiency.parse_upload_times(records, summer_day)
    assert len(parsed) == 10
    assert parsed[3][1]["load"] == 222.0


def test_unparseable_timestamps_are_dropped_not_fatal(summer_day):
    records = wall_clock_records(summer_day, 5)
    records.append({"uploadTime": "not a timestamp"})
    records.append({"load": 100.0})  # no uploadTime at all
    parsed = efficiency.parse_upload_times(records, summer_day)
    assert len(parsed) == 5
