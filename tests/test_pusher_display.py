"""Tests for the AWTRIX display formatting.

Also the canary that `pythonpath` covers the awtrix-pusher tree, not just
collector/.
"""

import pytest

from pusher import IDLE_W, build_apps, fmt_power, soc_color


@pytest.mark.parametrize("watts,expected", [
    (0, "0W"), (600, "600W"), (-600, "600W"), (999, "999W"),
    (1000, "1.0kW"), (1800, "1.8kW"), (-1800, "1.8kW"), (12345, "12.3kW"),
])
def test_fmt_power(watts, expected):
    assert fmt_power(watts) == expected


@pytest.mark.parametrize("soc,expected", [
    (100, "#00E000"), (60, "#00E000"), (59.9, "#FFD400"),
    (30, "#FFD400"), (29.9, "#FF3030"), (0, "#FF3030"),
])
def test_soc_color_ramp(soc, expected):
    assert soc_color(soc) == expected


NO_ICONS = {"soc": "", "pv": "", "grid": "", "load": ""}


def test_build_apps_signs_soc_by_battery_direction():
    """pbat is positive when discharging, so a charging battery reads '+'."""
    charging = build_apps({"soc_percent": 80.0, "battery_power_w": -1000.0},
                          False, NO_ICONS)
    discharging = build_apps({"soc_percent": 80.0, "battery_power_w": 1000.0},
                             False, NO_ICONS)
    assert charging["soc"]["text"] == "+80.0%"
    assert discharging["soc"]["text"] == "-80.0%"


def test_build_apps_leaves_soc_unsigned_within_the_idle_band():
    """Sensor noise around zero must not flicker the sign."""
    apps = build_apps({"soc_percent": 80.0, "battery_power_w": IDLE_W - 1},
                      False, NO_ICONS)
    assert apps["soc"]["text"] == "80.0%"


def test_build_apps_drops_the_decimal_at_full_charge():
    apps = build_apps({"soc_percent": 100.0}, False, NO_ICONS)
    assert apps["soc"]["text"] == "100%"


def test_build_apps_colours_grid_by_direction():
    importing = build_apps({"grid_power_w": 1000.0}, False, NO_ICONS)
    exporting = build_apps({"grid_power_w": -1000.0}, False, NO_ICONS)
    idle = build_apps({"grid_power_w": 5.0}, False, NO_ICONS)
    assert importing["grid"]["color"] == "#FF3030"
    assert exporting["grid"]["color"] == "#00E000"
    assert idle["grid"]["color"] == "#888888"


def test_stale_data_dims_every_app():
    fields = {"soc_percent": 80.0, "pv_power_w": 1500.0,
              "grid_power_w": -200.0, "load_power_w": 800.0}
    apps = build_apps(fields, True, NO_ICONS)
    assert len(apps) == 4
    assert all(app["color"] == "#555555" for app in apps.values())


def test_icon_replaces_the_text_label():
    with_icon = build_apps({"pv_power_w": 1500.0}, False, {"pv": "solar"})
    without = build_apps({"pv_power_w": 1500.0}, False, NO_ICONS)
    assert with_icon["pv"] == {"text": "1.5kW", "color": "#FFD400", "icon": "solar"}
    assert without["pv"]["text"] == "PV 1.5kW"
    assert "icon" not in without["pv"]


def test_missing_fields_produce_no_app():
    """A partial power_readings point must not render a blank tile."""
    assert build_apps({}, False, NO_ICONS) == {}
    assert set(build_apps({"pv_power_w": 100.0}, False, NO_ICONS)) == {"pv"}
