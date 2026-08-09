"""The loss arithmetic, and the two data defects it has to survive.

`conversion_loss_kwh` is a difference of two large, nearly equal numbers -- a
day's house load measured two ways, ~19 kWh each, differing by ~2 kWh. That
makes it unusually sensitive to anything that biases either integral, which is
why the despike filter and the resolution diagnostic are tested here alongside
the arithmetic rather than treated as incidental.
"""

import datetime as dt

import pytest

import efficiency
import pricing
from conftest import constant_samples
from prices import NL_TZ
from pricing import Sample

STEP_MIN = 5
STEPS = 288  # a full day of 5-minute records


def metered_records(day: dt.date, loads, soc: float = 50.0) -> list[dict]:
    """5-minute records with the given loads, starting at local midnight."""
    start = dt.datetime.combine(day, dt.time())
    return [
        {
            "uploadTime": (start + dt.timedelta(minutes=STEP_MIN * i)).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "load": load,
            "cbat": soc,
            "feedIn": 0.0,
            "gridCharge": 0.0,
        }
        for i, load in enumerate(loads)
    ]


def readings_matching(parsed, load: float, soc: float = 50.0, step_s: int = 30):
    """30-second samples spanning exactly the metered series' span."""
    return constant_samples(parsed[0][0], parsed[-1][0], grid=load, step_s=step_s, soc=soc)


ENERGY = {"eCharge": 30.0, "eDischarge": 28.0, "epv": 20.0,
          "eOutput": 10.0, "eInput": 12.0, "eGridCharge": 8.0}


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------

def test_conversion_loss_is_derived_minus_metered(summer_day):
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    readings = readings_matching(parsed, 1000.0)
    result = efficiency.compute_day(summer_day, parsed, ENERGY, readings)

    span_h = (parsed[-1][0] - parsed[0][0]).total_seconds() / 3600.0
    assert result["metered_load_kwh"] == pytest.approx(900.0 * span_h / 1000.0, abs=1e-3)
    assert result["derived_load_kwh"] == pytest.approx(1000.0 * span_h / 1000.0, abs=1e-3)
    assert result["conversion_loss_kwh"] == pytest.approx(100.0 * span_h / 1000.0, abs=1e-3)


def test_integration_is_trapezoidal_not_rectangular(summer_day):
    """A linear ramp is where the two rules visibly disagree.

    Left-rectangle integration of a 0->W ramp under-counts by exactly half a
    step. On a real load curve that bias is invisible; against a difference of
    two ~19 kWh integrals it is not.
    """
    loads = [1000.0 * i / (STEPS - 1) for i in range(STEPS)]
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    win_start, win_end = pricing.day_window_utc(summer_day)
    metered = efficiency.metered_samples(parsed)
    span_h = (parsed[-1][0] - parsed[0][0]).total_seconds() / 3600.0

    got = efficiency.net_kwh(metered, lambda s: s.load, win_start, win_end)
    assert got == pytest.approx(500.0 * span_h / 1000.0, abs=1e-6)


def test_charge_minus_discharge_is_not_called_a_loss(summer_day):
    """It only is one over a SoC-matched window, and this day is not one."""
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 1000.0))
    assert result["charge_minus_discharge_kwh"] == pytest.approx(2.0)
    assert "battery_loss_kwh" not in result


def test_battery_loss_is_absent_not_zero_without_a_capacity(summer_day, monkeypatch):
    """A zero here would read as a lossless battery -- the most flattering
    possible answer, arrived at by not knowing something."""
    monkeypatch.setattr(efficiency, "BATTERY_CAPACITY_KWH", None)
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 1000.0))
    assert "battery_loss_kwh" not in result
    assert "delta_soc_kwh" not in result
    assert "total_loss_kwh" not in result


def test_battery_loss_corrects_for_the_soc_left_in_the_battery(summer_day, monkeypatch):
    monkeypatch.setattr(efficiency, "BATTERY_CAPACITY_KWH", "27.9")
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    # SoC rises 10 points across the day: 2.79 kWh went in and stayed in.
    readings = (constant_samples(parsed[0][0], parsed[len(parsed) // 2][0],
                                 grid=1000.0, soc=40.0)
                + constant_samples(parsed[len(parsed) // 2][0] + dt.timedelta(seconds=30),
                                   parsed[-1][0], grid=1000.0, soc=50.0))
    result = efficiency.compute_day(summer_day, parsed, ENERGY, readings)

    assert result["delta_soc_percent"] == pytest.approx(10.0)
    assert result["delta_soc_kwh"] == pytest.approx(2.79)
    assert result["battery_loss_kwh"] == pytest.approx(30.0 - 28.0 - 2.79)
    assert result["total_loss_kwh"] == pytest.approx(
        result["conversion_loss_kwh"] + result["battery_loss_kwh"])


def test_computed_at_is_when_the_job_ran_not_the_day_it_describes(summer_day):
    """Every staleness check reads this field, because a daily_energy row's own
    timestamp is 51 hours old on a healthy system right before the next run."""
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 1000.0))
    day_start = pricing.day_window_utc(summer_day)[0].timestamp()
    assert result["computed_at_unix"] > day_start + 86400


# --------------------------------------------------------------------------
# The resolution diagnostic
# --------------------------------------------------------------------------

def test_resampled_derived_matches_when_the_signal_is_flat(summer_day):
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 1000.0))
    assert result["derived_load_kwh_at_5m"] == pytest.approx(
        result["derived_load_kwh"], abs=1e-6)


def test_resampled_derived_diverges_on_a_signal_the_5_minute_grid_cannot_see(summer_day):
    """The point of the diagnostic: it measures the resolution artefact instead
    of letting us assume there isn't one.

    A square wave whose period is exactly the metered cadence is invisible at
    5-minute resolution -- resampling lands on one phase every time -- while
    the 30 s series integrates its true mean.
    """
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    start, end = parsed[0][0], parsed[-1][0]
    readings, t, high = [], start, True
    while t <= end:
        readings.append(Sample(time=t, pv=0.0, grid=0.0,
                               load=2000.0 if high else 0.0, battery=0.0, soc=50.0))
        if (t - start).total_seconds() % 150 == 0:
            high = not high
        t += dt.timedelta(seconds=30)
    result = efficiency.compute_day(summer_day, parsed, ENERGY, readings)

    assert abs(result["derived_load_kwh"] - result["derived_load_kwh_at_5m"]) > 1.0


# --------------------------------------------------------------------------
# Despiking
# --------------------------------------------------------------------------

def test_an_isolated_metered_spike_is_dropped(summer_day):
    """The 2026-08-01 defect: 5832 W in one record against ~500 W everywhere
    else, worth ~0.5 kWh of phantom load apiece."""
    loads = [500.0] * STEPS
    loads[100] = 5832.0
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    readings = readings_matching(parsed, 550.0)
    result = efficiency.compute_day(summer_day, parsed, ENERGY, readings)

    assert result["series_dropped"] == 1
    assert result["series_count"] == STEPS
    span_h = (parsed[-1][0] - parsed[0][0]).total_seconds() / 3600.0
    assert result["metered_load_kwh"] == pytest.approx(500.0 * span_h / 1000.0, abs=0.02)


def test_a_real_appliance_spike_survives(summer_day):
    """It appears in both series, so it is not a defect -- and a filter that
    looked only at neighbouring metered records would have eaten it."""
    loads = [500.0] * STEPS
    loads[100] = 5832.0
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    readings = readings_matching(parsed, 500.0)
    for sample in readings:
        if abs((sample.time - parsed[100][0]).total_seconds()) <= 150:
            sample.load = 6000.0
    result = efficiency.compute_day(summer_day, parsed, ENERGY, readings)
    assert result["series_dropped"] == 0


def test_records_with_no_derived_samples_nearby_are_kept(summer_day):
    """Nothing to judge them against. A filter that discards what it cannot
    check would eat exactly the outages the coverage gate exists to catch."""
    loads = [500.0] * STEPS
    loads[100] = 5832.0
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    readings = [s for s in readings_matching(parsed, 500.0)
                if abs((s.time - parsed[100][0]).total_seconds()) > 600]
    kept, dropped = efficiency.drop_implausible(efficiency.metered_samples(parsed), readings)
    assert dropped == []
    assert len(kept) == STEPS


def test_despiking_can_be_disabled(summer_day, monkeypatch):
    monkeypatch.setattr(efficiency, "SPIKE_FACTOR", 0.0)
    loads = [500.0] * STEPS
    loads[100] = 5832.0
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 500.0))
    assert result["series_dropped"] == 0


def test_dropped_records_are_still_written_to_the_series(summer_day):
    """The filter applies to the integral, not to the archive. Raw upstream
    data is what makes a later recompute possible without re-fetching from a
    rate-limited API."""
    loads = [500.0] * STEPS
    loads[100] = 5832.0
    parsed = efficiency.parse_upload_times(metered_records(summer_day, loads), summer_day)
    points = efficiency.series_points(parsed, "SN1")
    assert len(points) == STEPS


# --------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------

def test_daily_payload_maps_to_exactly_the_api_fields(summer_day):
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * STEPS), summer_day)
    result = efficiency.compute_day(summer_day, parsed, ENERGY,
                                    readings_matching(parsed, 1000.0))
    api_fields = {k: v for k, v in result.items() if k.endswith("_api")}
    assert api_fields == {
        "charge_kwh_api": 30.0, "discharge_kwh_api": 28.0, "pv_kwh_api": 20.0,
        "export_kwh_api": 10.0, "import_kwh_api": 12.0, "grid_charge_kwh_api": 8.0,
    }


def test_the_series_never_stores_ppv_or_the_charging_pile(summer_day):
    """ppv reads 0.0 on every record on this system -- three separate full days
    summed to 0.00 kWh while the daily endpoint reported 17-25 kWh for the same
    days. Stored, it renders as a night that never ends.

    This test exists to fail the day someone helpfully "fixes" the missing PV
    column.
    """
    records = metered_records(summer_day, [900.0] * 3)
    for r in records:
        r["ppv"] = 0.0
        r["pchargingPile"] = 0
    parsed = efficiency.parse_upload_times(records, summer_day)
    fields = set()
    for point in efficiency.series_points(parsed, "SN1"):
        fields |= set(point._fields)
    assert fields == {"metered_load_w", "metered_soc_percent", "feed_in_w", "grid_charge_w"}


def test_series_points_carry_their_provenance(summer_day):
    """Two AlphaESS endpoints report overlapping quantities that disagree, so
    which one a number came from has to survive into the database."""
    parsed = efficiency.parse_upload_times(
        metered_records(summer_day, [900.0] * 3), summer_day)
    point = efficiency.series_points(parsed, "SN1")[0]
    assert point._tags == {"sys_sn": "SN1", "source": "getOneDayPowerBySn"}


def test_blank_energy_payloads_are_recognised():
    """HTTP 200, code 200, and all zeros: AlphaESS's overnight quiet period.
    Unmarshalled naively it becomes a legitimate-looking day of no energy,
    which is then written, marked done and never revisited."""
    assert efficiency._is_blank_energy(None)
    assert efficiency._is_blank_energy({})
    assert efficiency._is_blank_energy(dict.fromkeys(efficiency.ENERGY_KEYS, 0.0))
    assert not efficiency._is_blank_energy(ENERGY)


def test_local_midnight_timestamp_survives_the_winter(monkeypatch):
    """daily_cost and daily_energy must agree on what "a day" is stamped at."""
    for day in (dt.date(2026, 7, 17), dt.date(2026, 1, 17)):
        point = efficiency.daily_point(day, {"conversion_loss_kwh": 1.0}, "SN1")
        assert point._time == pricing.day_window_utc(day)[0]
        assert point._time.astimezone(NL_TZ).hour == 0
