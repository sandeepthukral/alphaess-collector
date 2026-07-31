"""Tests for the complete-day quality gate in pricing.py.

The gate is the only thing standing between a partially-observed day and a
`daily_cost` row that is treated as fact forever after -- `_already_done` never
revisits a day that was written.
"""

import datetime as dt

import pytest

import pricing
from conftest import constant_samples, hourly_intervals, quarter_hour_intervals
from pricing import compute_day, gate, priced_seconds

UTC = dt.UTC


# --------------------------------------------------------------------------
# priced_seconds
# --------------------------------------------------------------------------

def test_priced_seconds_full_day(summer_day):
    start, end = pricing.day_window_utc(summer_day)
    assert priced_seconds(hourly_intervals(start, 24), start, end) == 24 * 3600


def test_priced_seconds_partial_day(summer_day):
    start, end = pricing.day_window_utc(summer_day)
    assert priced_seconds(hourly_intervals(start, 12), start, end) == 12 * 3600


def test_priced_seconds_merges_duplicate_intervals(summer_day):
    """A duplicated hour must not compensate for a missing one.

    InfluxDB holds one point per (measurement, tag set, timestamp), so a price
    slot rewritten under a different tag set yields two intervals for the same
    hour. Summing durations would report a full day while an hour is missing.
    """
    start, end = pricing.day_window_utc(summer_day)
    intervals = hourly_intervals(start, 23) + hourly_intervals(start, 1)
    assert priced_seconds(intervals, start, end) == 23 * 3600


def test_priced_seconds_clips_to_the_window(summer_day):
    """Prices for neighbouring days must not count toward this day."""
    start, end = pricing.day_window_utc(summer_day)
    intervals = hourly_intervals(start - dt.timedelta(hours=3), 30)
    assert priced_seconds(intervals, start, end) == 24 * 3600


def test_priced_seconds_ignores_intervals_entirely_outside(summer_day):
    start, end = pricing.day_window_utc(summer_day)
    intervals = hourly_intervals(end, 5)
    assert priced_seconds(intervals, start, end) == 0


def test_priced_seconds_no_intervals(summer_day):
    start, end = pricing.day_window_utc(summer_day)
    assert priced_seconds([], start, end) == 0


# --------------------------------------------------------------------------
# compute_day + gate
# --------------------------------------------------------------------------

def test_fully_priced_day_is_accepted_and_costed(summer_day, day_window):
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, hourly_intervals(start, 24, total=0.10),
                         summer_day)
    assert result["price_coverage"] == 1.0
    assert result["coverage"] == 1.0
    # 1000 W for 24 h = 24 kWh at 0.10 EUR/kWh.
    assert result["cost_model1"] == pytest.approx(2.40)
    assert gate(result)[0] is True


def test_half_priced_day_is_rejected(summer_day, day_window):
    """Regression: a day priced for 12 of 24 hours costed out at exactly half
    the true figure while `coverage` still read 1.000 and the gate passed it.

    That is the failure mode this gate exists for -- the number is wrong, not
    noisy, and nothing downstream would ever question it.
    """
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, hourly_intervals(start, 12, total=0.10),
                         summer_day)

    assert result["coverage"] == 1.0           # samples were perfect...
    assert result["price_coverage"] == 0.5     # ...prices were not
    assert result["cost_model1"] == pytest.approx(1.20)  # half of 2.40

    ok, why = gate(result)
    assert ok is False
    assert "price coverage" in why
    assert "12.0h" in why  # names how much of the day is unpriced


def test_day_missing_a_single_hour_is_rejected(summer_day, day_window):
    """The realistic case: late-published day-ahead prices leave one hole."""
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    intervals = (hourly_intervals(start, 8)
                 + hourly_intervals(start + dt.timedelta(hours=9), 15))
    result = compute_day(samples, intervals, summer_day)
    assert result["price_coverage"] == pytest.approx(23 / 24, abs=1e-4)
    assert gate(result)[0] is False


def test_gate_still_rejects_on_sample_coverage(summer_day, day_window):
    """The new check must not shadow the pre-existing one."""
    start, _end = day_window
    # Only the middle 12 hours were sampled at all.
    samples = constant_samples(start + dt.timedelta(hours=6),
                               start + dt.timedelta(hours=18), grid=1000.0)
    result = compute_day(samples, hourly_intervals(start, 24), summer_day)
    assert result["price_coverage"] == 1.0
    ok, why = gate(result)
    assert ok is False
    assert why.startswith("coverage")


def test_gate_rejects_on_max_gap(summer_day, day_window):
    start, end = day_window
    samples = (constant_samples(start, start + dt.timedelta(hours=11), grid=1000.0)
               + constant_samples(start + dt.timedelta(hours=13), end, grid=1000.0))
    result = compute_day(samples, hourly_intervals(start, 24), summer_day)
    assert result["max_gap_s"] == pytest.approx(2 * 3600)
    ok, why = gate(result)
    assert ok is False
    # Sample coverage drops below MIN_COVERAGE first for a 2 h hole; either
    # reason is correct, but it must not pass.
    assert "coverage" in why or "max gap" in why


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------

@pytest.mark.parametrize("day,hours", [
    (dt.date(2026, 3, 29), 23),   # spring forward, Europe/Amsterdam
    (dt.date(2026, 10, 25), 25),  # fall back
])
def test_price_coverage_is_correct_on_dst_days(day, hours):
    """A 23/25-hour day must not be scored against a hardcoded 24."""
    start, end = pricing.day_window_utc(day)
    assert (end - start).total_seconds() == hours * 3600
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, hourly_intervals(start, hours), day)
    assert result["price_coverage"] == 1.0
    assert gate(result)[0] is True


# --------------------------------------------------------------------------
# 15-minute settlement (contract cutover 2026-08-01)
#
# The gate and integration logic derive slot length from each interval's own
# from/till, so nothing here should require production changes -- these
# mirror the hourly cases above at the resolution the contract moves to, to
# prove that rather than assume it.
# --------------------------------------------------------------------------

def test_priced_seconds_full_day_at_quarter_hour_resolution(summer_day):
    start, end = pricing.day_window_utc(summer_day)
    assert priced_seconds(quarter_hour_intervals(start, 96), start, end) == 24 * 3600


def test_fully_priced_day_is_accepted_and_costed_at_quarter_hour_resolution(
        summer_day, day_window):
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, quarter_hour_intervals(start, 96, total=0.10),
                         summer_day)
    assert result["price_coverage"] == 1.0
    assert result["coverage"] == 1.0
    assert result["cost_model1"] == pytest.approx(2.40)  # same 24 kWh @ 0.10
    assert gate(result)[0] is True


def test_half_priced_day_is_rejected_at_quarter_hour_resolution(summer_day, day_window):
    """Same regression as test_half_priced_day_is_rejected, at 96-slot resolution."""
    start, end = day_window
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, quarter_hour_intervals(start, 48, total=0.10),
                         summer_day)

    assert result["coverage"] == 1.0
    assert result["price_coverage"] == 0.5
    assert result["cost_model1"] == pytest.approx(1.20)

    ok, why = gate(result)
    assert ok is False
    assert "price coverage" in why


@pytest.mark.parametrize("day,quarters", [
    (dt.date(2026, 3, 29), 92),    # spring forward: 23h x 4
    (dt.date(2026, 10, 25), 100),  # fall back: 25h x 4 -- the next DST
                                   # transition after the 2026-08-01 cutover
])
def test_price_coverage_is_correct_on_dst_days_at_quarter_hour_resolution(day, quarters):
    """A 92/100-quarter-hour day must not be scored against a hardcoded 96."""
    start, end = pricing.day_window_utc(day)
    assert (end - start).total_seconds() == quarters * 900
    samples = constant_samples(start, end, grid=1000.0)
    result = compute_day(samples, quarter_hour_intervals(start, quarters), day)
    assert result["price_coverage"] == 1.0
    assert gate(result)[0] is True
