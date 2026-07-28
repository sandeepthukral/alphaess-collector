"""MODEL_VERSION in pricing.py and the dashboard variable must agree.

Nothing at runtime couples them, and a mismatch fails silently in the worst
direction: pricing.py writes correct rows at the new version while every panel
keeps reading the old ones, so the dashboard shows stale numbers indefinitely
rather than going blank.
"""

import json
import pathlib

import pytest

from pricing import MODEL_VERSION

REPO = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "grafana" / "alphaess-battery-savings.json"


@pytest.fixture(scope="module")
def model_version_variable():
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    variables = [v for v in dash["templating"]["list"] if v["name"] == "model_version"]
    assert len(variables) == 1, "expected exactly one model_version variable"
    return variables[0]


def test_dashboard_defaults_to_the_current_model_version(model_version_variable):
    assert model_version_variable["current"]["value"] == MODEL_VERSION


def test_current_model_version_is_a_selectable_option(model_version_variable):
    options = model_version_variable["options"]
    selected = [o for o in options if o.get("selected")]
    assert [o["value"] for o in selected] == [MODEL_VERSION]
    assert MODEL_VERSION in model_version_variable["query"].split(",")


def test_every_daily_cost_panel_filters_on_the_variable():
    """An unfiltered panel would sum every model version at once."""
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    unfiltered = [
        panel.get("title")
        for panel in dash.get("panels", [])
        for target in panel.get("targets", [])
        if "daily_cost" in target.get("query", "")
        and "${model_version}" not in target.get("query", "")
    ]
    assert unfiltered == []
