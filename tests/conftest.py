"""Shared fixtures.

The services import each other flat (`from prices import ...`), matching the
Dockerfile layout where every module lands in /app. `pythonpath` in
pyproject.toml reproduces that, so tests import the same modules that run in
production rather than a repackaged copy.
"""

import datetime as dt

import pytest

import pricing
from pricing import Sample

UTC = dt.UTC


def hourly_intervals(start: dt.datetime, hours: int, total: float = 0.10,
                     market: float = 0.04, tax: float = 0.01,
                     markup: float = 0.02, energy_tax: float = 0.03) -> list[dict]:
    """`hours` consecutive one-hour price intervals from `start`."""
    return [
        {
            "from": start + dt.timedelta(hours=h),
            "till": start + dt.timedelta(hours=h + 1),
            "market_price": market,
            "market_price_tax": tax,
            "sourcing_markup": markup,
            "energy_tax": energy_tax,
            "total": total,
        }
        for h in range(hours)
    ]


def quarter_hour_intervals(start: dt.datetime, quarters: int, total: float = 0.10,
                          market: float = 0.04, tax: float = 0.01,
                          markup: float = 0.02, energy_tax: float = 0.03) -> list[dict]:
    """`quarters` consecutive 15-minute price intervals from `start`.

    Mirrors `hourly_intervals` at the resolution the contract moves to on
    2026-08-01.
    """
    return [
        {
            "from": start + dt.timedelta(minutes=15 * q),
            "till": start + dt.timedelta(minutes=15 * (q + 1)),
            "market_price": market,
            "market_price_tax": tax,
            "sourcing_markup": markup,
            "energy_tax": energy_tax,
            "total": total,
        }
        for q in range(quarters)
    ]


def constant_samples(start: dt.datetime, end: dt.datetime, *, grid: float,
                     step_s: int = 30, pv: float = 0.0, battery: float = 0.0,
                     soc: float = 50.0) -> list[Sample]:
    """Evenly spaced samples holding a constant power, inclusive of `end`."""
    samples = []
    t = start
    while t < end:
        samples.append(Sample(time=t, pv=pv, grid=grid, load=pv + grid + battery,
                              battery=battery, soc=soc))
        t += dt.timedelta(seconds=step_s)
    samples.append(Sample(time=end, pv=pv, grid=grid, load=pv + grid + battery,
                          battery=battery, soc=soc))
    return samples


@pytest.fixture
def summer_day():
    """A plain 24-hour local day, well away from any DST transition."""
    return dt.date(2026, 7, 17)


@pytest.fixture
def day_window(summer_day):
    return pricing.day_window_utc(summer_day)
