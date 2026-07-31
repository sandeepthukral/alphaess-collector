"""Tests for the EnergyZero-backed quarter-hour reconstruction fallback.

Covers `reconstruct_quarter_hour_rows`, the pure function used when Frank's
own API keeps returning hourly-or-coarser rows past the 2026-08-01 cutover
(see DESIGN-battery-savings.md). No network calls here -- `fetch_prices_for_day`
and `fetch_quarter_hour_wholesale` are exercised live, manually, per the plan's
verification step, not mocked in unit tests.
"""

import datetime as dt

import pytest

import prices

UTC = dt.UTC


def _hourly_row(start: dt.datetime, market_price: float, tax_ratio: float = 0.21,
                 markup: float = 0.01815, energy_tax: float = 0.11085,
                 duration_s: float = 3600.0) -> dict:
    tax = market_price * tax_ratio
    return {
        "market_price": market_price,
        "market_price_tax": tax,
        "sourcing_markup": markup,
        "energy_tax": energy_tax,
        "total": round(market_price + tax + markup + energy_tax, 6),
        "from": start,
        "till": start + dt.timedelta(seconds=duration_s),
        "duration_s": duration_s,
    }


def _quarters(start: dt.datetime, prices_: list[float]) -> list[dict]:
    return [
        {
            "from": start + dt.timedelta(minutes=15 * i),
            "till": start + dt.timedelta(minutes=15 * (i + 1)),
            "wholesale_price": p,
        }
        for i, p in enumerate(prices_)
    ]


def test_reconstruction_splits_one_hour_into_four_quarters():
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    hourly = [_hourly_row(start, market_price=0.15)]
    wholesale = _quarters(start, [0.15, 0.15, 0.15, 0.15])  # flat wholesale shape

    out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    assert len(out) == 4
    for row in out:
        assert row["duration_s"] == 900.0
        assert row["market_price"] == 0.15
        # flat wholesale shape reproduces the original hourly total exactly
        assert row["total"] == hourly[0]["total"]


def test_reconstruction_follows_real_wholesale_shape_not_a_flat_split():
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    quarter_prices = [0.10, 0.20, 0.05, 0.25]  # avg = 0.15, matching the hourly row
    hourly = [_hourly_row(start, market_price=0.15)]
    wholesale = _quarters(start, quarter_prices)

    out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    assert [round(r["market_price"], 6) for r in out] == quarter_prices
    # None of the quarters is just the hourly total split 4 ways.
    flat_split_total = round(hourly[0]["total"] / 4, 6)
    assert all(round(r["total"], 6) != flat_split_total for r in out)


def test_reconstruction_preserves_btw_ratio_per_quarter():
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    hourly = [_hourly_row(start, market_price=0.20, tax_ratio=0.21)]
    wholesale = _quarters(start, [0.10, 0.30, 0.20, 0.40])

    out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    for row in out:
        assert row["market_price_tax"] == pytest.approx(row["market_price"] * 0.21)


def test_reconstruction_carries_flat_components_through_unchanged():
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    hourly = [_hourly_row(start, market_price=0.20, markup=0.01815, energy_tax=0.11085)]
    wholesale = _quarters(start, [0.10, 0.30, 0.20, 0.40])

    out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    for row in out:
        assert row["sourcing_markup"] == 0.01815
        assert row["energy_tax"] == 0.11085


def test_missing_wholesale_quarters_passes_row_through_unchanged(caplog):
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    hourly = [_hourly_row(start, market_price=0.15)]
    wholesale: list[dict] = []  # EnergyZero gap/outage

    with caplog.at_level("WARNING"):
        out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    assert out == hourly
    assert any("Not reconstructing" in r.message for r in caplog.records)


def test_partial_wholesale_quarters_passes_row_through_unchanged(caplog):
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    hourly = [_hourly_row(start, market_price=0.15)]
    wholesale = _quarters(start, [0.10, 0.20])  # only 2 of the 4 needed

    with caplog.at_level("WARNING"):
        out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    assert out == hourly
    assert any("Not reconstructing" in r.message for r in caplog.records)


def test_reconstruction_matches_non_hourly_slot_by_containment_not_a_fixed_count():
    """The matching is containment-based, not a hardcoded '4 per hour' --
    this is what makes it correct on 23h/25h DST days without special-casing."""
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    # A 90-minute coarse row (as could appear right at a ragged cutover boundary)
    # spanning 6 real quarter-hours.
    hourly = [_hourly_row(start, market_price=0.15, duration_s=5400.0)]
    wholesale = _quarters(start, [0.10, 0.20, 0.30, 0.05, 0.15, 0.20])

    out = prices.reconstruct_quarter_hour_rows(hourly, wholesale)

    assert len(out) == 6
    assert all(r["duration_s"] == 900.0 for r in out)


def test_reconstruction_has_no_special_case_for_already_fine_rows():
    """`reconstruct_quarter_hour_rows` is purely mechanical containment
    matching -- it does not know or care whether a row was already
    quarter-hourly. `run()` is what must only ever pass it genuinely coarse
    rows (duration_s >= 3600); this test pins down why that filtering has
    to live in the caller, not here."""
    start = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    already_fine = _hourly_row(start, market_price=0.15, duration_s=900.0)
    wholesale = _quarters(start, [0.999])  # deliberately different, to prove it's used

    out = prices.reconstruct_quarter_hour_rows([already_fine], wholesale)

    assert len(out) == 1
    assert out[0]["market_price"] == 0.999
