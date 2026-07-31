"""Tests for the energy integration in pricing.py.

These are the functions that turn power samples into euros, so an error here is
silent and expensive: the number still looks plausible on the dashboard.
"""

import datetime as dt

import pytest

from conftest import constant_samples, hourly_intervals, quarter_hour_intervals
from pricing import _accumulate, export_price, import_price, integrate_by_interval

UTC = dt.UTC
T0 = dt.datetime(2026, 7, 17, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# _accumulate
# --------------------------------------------------------------------------

def test_accumulate_constant_import():
    bucket = [0.0, 0.0]
    _accumulate(bucket, 1.0, 1000.0, 1000.0)
    assert bucket == [1000.0, 0.0]


def test_accumulate_constant_export():
    bucket = [0.0, 0.0]
    _accumulate(bucket, 1.0, -1000.0, -1000.0)
    assert bucket == [0.0, 1000.0]


def test_accumulate_ramp_without_sign_change_uses_trapezoid():
    bucket = [0.0, 0.0]
    _accumulate(bucket, 2.0, 0.0, 1000.0)
    assert bucket[0] == pytest.approx(1000.0)  # mean 500 W over 2 h
    assert bucket[1] == 0.0


def test_accumulate_splits_at_zero_crossing():
    """A ramp +1000 -> -1000 over 1 h is half import, half export.

    Summing it as one trapezoid would give zero for both, hiding a full hour of
    grid traffic and its cost, because import and export are priced differently.
    """
    bucket = [0.0, 0.0]
    _accumulate(bucket, 1.0, 1000.0, -1000.0)
    assert bucket[0] == pytest.approx(250.0)
    assert bucket[1] == pytest.approx(250.0)


def test_accumulate_asymmetric_zero_crossing():
    # +900 -> -100 over 1 h: crosses at t=0.9, areas 405 Wh in / 5 Wh out.
    bucket = [0.0, 0.0]
    _accumulate(bucket, 1.0, 900.0, -100.0)
    assert bucket[0] == pytest.approx(405.0)
    assert bucket[1] == pytest.approx(5.0)


def test_accumulate_ignores_non_positive_duration():
    bucket = [0.0, 0.0]
    _accumulate(bucket, 0.0, 1000.0, 1000.0)
    _accumulate(bucket, -1.0, 1000.0, 1000.0)
    assert bucket == [0.0, 0.0]


@pytest.mark.parametrize("ps,pe", [
    (1000.0, -1000.0), (-1000.0, 1000.0), (900.0, -100.0), (-5.0, 5.0),
    (0.0, -500.0), (0.0, 500.0), (250.0, 250.0), (-250.0, -250.0),
    (1e-9, -1e-9),
])
def test_accumulate_net_always_matches_the_trapezoid(ps, pe):
    """import - export must equal the plain signed integral, crossing or not.

    The zero-crossing branch computes two triangles instead of one trapezoid;
    if that algebra drifts, the split still looks reasonable while the total no
    longer conserves energy.
    """
    bucket = [0.0, 0.0]
    _accumulate(bucket, 1.0, ps, pe)
    assert bucket[0] - bucket[1] == pytest.approx((ps + pe) / 2, abs=1e-9)
    assert bucket[0] >= 0 and bucket[1] >= 0


# --------------------------------------------------------------------------
# integrate_by_interval
# --------------------------------------------------------------------------

def test_integrate_assigns_energy_to_the_right_hour():
    intervals = hourly_intervals(T0, 3)
    samples = constant_samples(T0, T0 + dt.timedelta(hours=3), grid=1000.0,
                               step_s=300)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    for imp, exp in result:
        assert imp == pytest.approx(1000.0)  # 1000 W for 1 h
        assert exp == pytest.approx(0.0)


def test_integrate_splits_a_segment_spanning_an_hour_boundary():
    """Samples need not align to interval edges; the segment must be cut."""
    intervals = hourly_intervals(T0, 2)
    samples = constant_samples(T0 + dt.timedelta(minutes=30),
                               T0 + dt.timedelta(minutes=90),
                               grid=1000.0, step_s=3600)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    assert result[0][0] == pytest.approx(500.0)
    assert result[1][0] == pytest.approx(500.0)


def test_integrate_drops_energy_outside_every_priced_interval():
    """Documents the behaviour that makes the price-coverage gate necessary.

    Energy in an unpriced hour is not an approximation, it is discarded. The
    guard against that lives in compute_day/gate, not here -- this test pins
    the premise so the guard cannot be removed as redundant.
    """
    intervals = hourly_intervals(T0, 1)  # only the first hour is priced
    samples = constant_samples(T0, T0 + dt.timedelta(hours=2), grid=1000.0,
                               step_s=300)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    assert len(result) == 1
    assert result[0][0] == pytest.approx(1000.0)  # second hour silently gone


def test_integrate_counterfactual_adds_battery_to_grid():
    """Model 2: whatever the battery discharged would have been imported."""
    intervals = hourly_intervals(T0, 1)
    samples = constant_samples(T0, T0 + dt.timedelta(hours=1), grid=0.0,
                               battery=1000.0, step_s=300)
    actual = integrate_by_interval(samples, lambda s: s.grid, intervals)
    counterfactual = integrate_by_interval(
        samples, lambda s: s.grid + s.battery, intervals)
    assert actual[0] == [0.0, 0.0]
    assert counterfactual[0][0] == pytest.approx(1000.0)


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------

def test_import_price_is_the_all_in_total():
    iv = hourly_intervals(T0, 1, total=0.31)[0]
    assert import_price(iv) == 0.31


def test_export_price_deducts_the_sourcing_markup():
    """Pins the saldering choice from DESIGN-battery-savings.md (option b):
    commodity credited, markup deducted, energy tax refunded, BTW kept."""
    iv = hourly_intervals(T0, 1, market=0.04, tax=0.01, markup=0.02,
                          energy_tax=0.03)[0]
    assert export_price(iv) == pytest.approx(0.04 + 0.01 - 0.02 + 0.03)


# --------------------------------------------------------------------------
# 15-minute settlement (contract cutover 2026-08-01)
# --------------------------------------------------------------------------

def test_integrate_assigns_energy_to_the_right_quarter_hour():
    intervals = quarter_hour_intervals(T0, 3)
    samples = constant_samples(T0, T0 + dt.timedelta(minutes=45), grid=1000.0,
                               step_s=60)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    for imp, exp in result:
        assert imp == pytest.approx(250.0)  # 1000 W for 15 min
        assert exp == pytest.approx(0.0)


def test_integrate_splits_a_segment_spanning_a_quarter_hour_boundary():
    """Mirrors the hour-boundary test at 15-minute resolution."""
    intervals = quarter_hour_intervals(T0, 2)
    samples = constant_samples(T0 + dt.timedelta(minutes=7, seconds=30),
                               T0 + dt.timedelta(minutes=22, seconds=30),
                               grid=1000.0, step_s=900)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    assert result[0][0] == pytest.approx(125.0)   # 1000 W for 7.5 min
    assert result[1][0] == pytest.approx(125.0)


def test_integrate_by_interval_handles_non_uniform_slot_durations():
    """The cutover day itself is uniform (all-hourly or all-15-min, never
    mixed, since the contract switches exactly at a local-midnight day
    boundary) -- but `boundaries = sorted(set(froms) | set(tills))` has never
    been exercised against a non-uniform grid before. A 15-min slot followed
    by a 45-min slot proves it doesn't assume every interval is the same
    length."""
    intervals = [
        {"from": T0, "till": T0 + dt.timedelta(minutes=15)},
        {"from": T0 + dt.timedelta(minutes=15), "till": T0 + dt.timedelta(minutes=60)},
    ]
    samples = constant_samples(T0, T0 + dt.timedelta(minutes=60), grid=1000.0,
                               step_s=300)
    result = integrate_by_interval(samples, lambda s: s.grid, intervals)
    assert result[0][0] == pytest.approx(250.0)   # 1000 W for 15 min
    assert result[1][0] == pytest.approx(750.0)   # 1000 W for 45 min
