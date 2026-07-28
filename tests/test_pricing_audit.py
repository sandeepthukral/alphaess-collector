"""Tests for `pricing.py --audit`.

The audit exists because a rerun cannot fix everything: --force overwrites a
day it accepts, but a day the gate now *rejects* keeps whatever the old rules
wrote. Those rows are invisible otherwise -- they look like every other row.
"""

import datetime as dt

from conftest import constant_samples, hourly_intervals
from pricing import NL_TZ, audit_day, stored_days


class FakeRecord:
    def __init__(self, time, values):
        self._time = time
        self.values = values

    def get_time(self):
        return self._time


class FakeTable:
    def __init__(self, records):
        self.records = records


class FakeQueryApi:
    def __init__(self, tables):
        self._tables = tables
        self.queries = []

    def query(self, flux):
        self.queries.append(flux)
        return self._tables


# --------------------------------------------------------------------------
# stored_days
# --------------------------------------------------------------------------

def test_stored_days_maps_the_row_timestamp_back_to_its_local_day():
    """Rows are stamped at local midnight; in summer that is 22:00 UTC the day
    before, so a naive .date() on the UTC instant names the wrong day."""
    midnight_local = dt.datetime(2026, 7, 17, tzinfo=NL_TZ)
    assert midnight_local.astimezone(dt.UTC).date() == dt.date(2026, 7, 16)

    api = FakeQueryApi([FakeTable([
        FakeRecord(midnight_local.astimezone(dt.UTC), {"model_version": "1"}),
    ])])
    assert stored_days(api, "alphaess", "SN1",
                       dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                       dt.datetime(2027, 1, 1, tzinfo=dt.UTC)) == [
        (dt.date(2026, 7, 17), "1")]


def test_stored_days_deduplicates_and_sorts():
    def row(day):
        return FakeRecord(dt.datetime(2026, 7, day, tzinfo=NL_TZ).astimezone(dt.UTC),
                          {"model_version": "1"})

    api = FakeQueryApi([FakeTable([row(18), row(17), row(18)])])
    assert stored_days(api, "alphaess", "SN1",
                       dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                       dt.datetime(2027, 1, 1, tzinfo=dt.UTC)) == [
        (dt.date(2026, 7, 17), "1"), (dt.date(2026, 7, 18), "1")]


def test_stored_days_skips_records_without_a_timestamp():
    api = FakeQueryApi([FakeTable([FakeRecord(None, {"model_version": "1"})])])
    assert stored_days(api, "alphaess", "SN1",
                       dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                       dt.datetime(2027, 1, 1, tzinfo=dt.UTC)) == []


def test_stored_days_filters_on_the_serial(summer_day):
    api = FakeQueryApi([FakeTable([])])
    stored_days(api, "alphaess", "AL5000TESTSN",
                dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                dt.datetime(2027, 1, 1, tzinfo=dt.UTC))
    assert 'r.sys_sn == "AL5000TESTSN"' in api.queries[0]
    assert 'r._measurement == "daily_cost"' in api.queries[0]


# --------------------------------------------------------------------------
# audit_day
# --------------------------------------------------------------------------

def test_audit_passes_a_fully_priced_day(summer_day, day_window):
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    status, detail = audit_day(summer_day, samples, hourly_intervals(start, 24))
    assert status == "ok"
    assert "price_coverage=1.000" in detail


def test_audit_flags_a_row_whose_day_is_only_half_priced(summer_day, day_window):
    """The row this whole change exists for: written under the old rules with
    half the true cost, and left in place by any later rerun."""
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    status, detail = audit_day(summer_day, samples, hourly_intervals(start, 12))
    assert status == "stale"
    assert "price coverage" in detail


def test_audit_flags_a_row_with_no_prices_left(summer_day, day_window):
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    status, detail = audit_day(summer_day, samples, [])
    assert status == "stale"
    assert "no prices" in detail


def test_audit_flags_a_row_with_no_samples_left(summer_day, day_window):
    start, _end = day_window
    status, detail = audit_day(summer_day, [], hourly_intervals(start, 24))
    assert status == "stale"
    assert "no power samples" in detail


def test_audit_flags_a_row_with_poor_sample_coverage(summer_day, day_window):
    start, _end = day_window
    samples = constant_samples(start + dt.timedelta(hours=6),
                               start + dt.timedelta(hours=18), grid=1000.0)
    status, _detail = audit_day(summer_day, samples, hourly_intervals(start, 24))
    assert status == "stale"
