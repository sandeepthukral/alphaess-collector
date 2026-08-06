"""A day is either trustworthy or absent -- daily_energy has no way to say
"this figure is shakier than the others".

The order the reasons are checked in is part of the contract, not an accident:
each one implies a different operator action, and the first reason reported is
the one that ends up in the Kuma notification.
"""

import pytest

import efficiency


def good() -> dict:
    return {
        "series_coverage": 0.999,
        "series_max_gap_s": 300.0,
        "series_count": 288,
        "series_dropped": 1,
        "readings_coverage": 0.999,
        "soc_align_median_pp": 0.0,
    }


def test_a_clean_day_passes():
    assert efficiency.gate(good()) == (True, "ok")


def test_thin_metered_series_is_rejected():
    """It fails in the flattering direction, which is why it is checked first:
    missing metered records under-state measured load and therefore over-state
    the loss. A too-good number is the one nobody investigates."""
    result = good() | {"series_coverage": 0.90}
    ok, why = efficiency.gate(result)
    assert not ok
    assert "series coverage" in why


def test_soc_misalignment_is_rejected_even_when_coverage_is_perfect():
    """The clock check. A day that is complete but timestamped an hour out is
    wrong, not thin, and the fix is --check-alignment rather than hunting a
    collector outage."""
    result = good() | {"soc_align_median_pp": 4.2}
    ok, why = efficiency.gate(result)
    assert not ok
    assert "soc_align" in why


def test_soc_misalignment_outranks_a_gap():
    """Both true at once must report the misalignment, because that is the one
    that makes the numbers wrong rather than merely noisy."""
    result = good() | {"soc_align_median_pp": 4.2, "series_max_gap_s": 9999.0}
    _, why = efficiency.gate(result)
    assert "soc_align" in why


def test_too_many_implausible_records_is_rejected():
    """Deleting a tenth of a day is not a repair. A handful of corrupt records
    is normal and handled; a large fraction means the payload is not to be
    trusted at all."""
    result = good() | {"series_dropped": 40}
    ok, why = efficiency.gate(result)
    assert not ok
    assert "implausible" in why


def test_the_observed_worst_real_day_still_passes():
    """2026-08-01: 14 implausible records in 288 (4.9%), the worst seen in 19
    days. Its corrected conversion loss (1.27 kWh) sits squarely among its
    neighbours', so the threshold must not exclude it."""
    result = good() | {"series_dropped": 14, "series_count": 288}
    assert efficiency.gate(result)[0]


def test_thin_power_readings_are_rejected():
    result = good() | {"readings_coverage": 0.5}
    ok, why = efficiency.gate(result)
    assert not ok
    assert "readings coverage" in why


def test_a_long_gap_in_the_metered_series_is_rejected():
    result = good() | {"series_max_gap_s": 7200.0}
    ok, why = efficiency.gate(result)
    assert not ok
    assert "max gap" in why


def test_the_readings_gate_is_the_same_one_pricing_uses():
    """Imported, not copied. Two jobs disagreeing about which days are complete
    would put a day in daily_energy but not daily_cost, or the reverse, with
    nothing in either row explaining why."""
    import pricing
    assert efficiency.MIN_COVERAGE is pricing.MIN_COVERAGE


@pytest.mark.parametrize("field, value", [
    ("series_coverage", 0.5),
    ("soc_align_median_pp", 99.0),
    ("readings_coverage", 0.5),
    ("series_max_gap_s", 99999.0),
    ("series_dropped", 200),
])
def test_each_threshold_rejects_on_its_own(field, value):
    assert not efficiency.gate(good() | {field: value})[0]
