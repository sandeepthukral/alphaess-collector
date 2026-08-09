"""What the nightly savings job writes, and what it refuses to write.

The write path had no test at all: every existing pricing test stops at
compute_day or gate, so the shape of the stored row -- its tags, its timestamp,
and the fields the monitoring depends on -- was pinned nowhere. That is the
part an outside reader (a dashboard, an alert, a Kuma monitor) actually
consumes.

Drives the real `process_day` rather than a reimplementation, with a fake write
API at the same seam production uses.
"""

import datetime as dt

import pytest

import pricing
from conftest import constant_samples, hourly_intervals

SYS_SN = "SN1"


class FakeWriteApi:
    """Mirrors tests/test_efficiency_write.py, so both jobs' write tests read
    alike and a fix to one is obviously applicable to the other."""

    def __init__(self):
        self.batches = []

    def write(self, bucket=None, record=None):
        self.batches.append(record if isinstance(record, list) else [record])

    def points(self, measurement):
        return [p for batch in self.batches for p in batch
                if p._name == measurement]


@pytest.fixture
def write_api():
    return FakeWriteApi()


@pytest.fixture
def full_day(summer_day, day_window):
    """A day that comfortably passes every gate: constant import, fully priced."""
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    intervals = hourly_intervals(start, 24)
    return summer_day, samples, intervals


def run(day, samples, intervals, write_api, dry_run=False):
    pricing.process_day(day, samples, intervals, dry_run,
                        (write_api, "alphaess", SYS_SN))


def test_a_good_day_writes_one_daily_cost_row(full_day, write_api):
    run(*full_day, write_api)
    assert len(write_api.points(pricing.DAILY_MEASUREMENT)) == 1


def test_every_daily_row_carries_computed_at_unix(full_day, write_api):
    """The staleness monitor reads this field and nothing else.

    A row without it is invisible to the monitoring, which is worse than a
    missing row: the dashboard keeps rendering and nothing says the job died.
    """
    day, _, _ = full_day
    before = dt.datetime.now(dt.UTC).timestamp()
    run(*full_day, write_api)
    point = write_api.points(pricing.DAILY_MEASUREMENT)[0]

    assert "computed_at_unix" in point._fields
    # Must be *now*, not the day it describes -- the whole point of the field.
    # The day under test is well in the past, so anything derived from it would
    # fall far below this bound.
    assert point._fields["computed_at_unix"] >= int(before)
    assert point._fields["computed_at_unix"] > pricing.day_window_utc(day)[0].timestamp()


def test_the_daily_row_is_tagged_and_stamped_at_local_midnight(full_day, write_api):
    """stored_days() maps the timestamp back through NL_TZ to recover the day,
    and the dashboard filters on both tags, so all three are load-bearing."""
    day, _, _ = full_day
    run(*full_day, write_api)
    point = write_api.points(pricing.DAILY_MEASUREMENT)[0]

    assert point._tags == {"sys_sn": SYS_SN, "model_version": pricing.MODEL_VERSION}
    assert point._time == pricing.day_window_utc(day)[0]


def test_the_row_carries_the_computed_fields(full_day, write_api):
    run(*full_day, write_api)
    fields = write_api.points(pricing.DAILY_MEASUREMENT)[0]._fields
    for name in ("cost_model1", "cost_model2", "saving", "load_kwh",
                 "coverage", "price_coverage"):
        assert name in fields


def test_dry_run_writes_nothing(full_day, write_api):
    run(*full_day, write_api, dry_run=True)
    assert write_api.batches == []


def test_a_gated_day_is_not_written(summer_day, day_window, write_api):
    """A half-priced day is wrong rather than thin -- the unpriced hours cost
    zero in both models -- so it must leave no row behind at all."""
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    half_priced = hourly_intervals(start, 12)

    run(summer_day, samples, half_priced, write_api)
    assert write_api.batches == []


def test_no_samples_writes_nothing(summer_day, day_window, write_api):
    start, _ = day_window
    run(summer_day, [], hourly_intervals(start, 24), write_api)
    assert write_api.batches == []


def test_no_prices_writes_nothing(summer_day, day_window, write_api):
    start, end = day_window
    run(summer_day, constant_samples(start, end, grid=1000.0), [], write_api)
    assert write_api.batches == []
