"""Each module's MODEL_VERSION and its dashboard's variable must agree.

Nothing at runtime couples them, and a mismatch fails silently in the worst
direction: the module writes correct rows at the new version while every panel
keeps reading the old ones, so the dashboard shows stale numbers indefinitely
rather than going blank.

Two jobs now carry a version -- pricing.py over `daily_cost` and efficiency.py
over `daily_energy` -- and the failure mode is identical, so they are
parametrized here rather than copied into a second file that would drift.
"""

import json
import pathlib

import pytest

import efficiency
import pricing

REPO = pathlib.Path(__file__).resolve().parent.parent

VERSIONED = [
    pytest.param(pricing.MODEL_VERSION, "alphaess-battery-savings.json", "daily_cost",
                 id="pricing"),
    pytest.param(efficiency.MODEL_VERSION, "alphaess-energy-losses.json", "daily_energy",
                 id="efficiency"),
    # The main dashboard's `Total saving to date` sums daily_cost, so it carries a third
    # copy of the same list and gets the same pins: unfiltered, that tile would add two
    # models' worth of euros together and the number would look entirely reasonable.
    #
    # Only pricing. The two jobs are at different versions (daily_cost 4, daily_energy 1),
    # so one `model_version` variable cannot serve both, and the main dashboard's is
    # pricing's -- the only daily_energy it reads is the nightly-jobs liveness pair, which
    # is exempt below for reasons that have nothing to do with which variable exists. A
    # panel there that ever SUMS daily_energy needs its own variable, not this one.
    pytest.param(pricing.MODEL_VERSION, "alphaess-dashboard.json", "daily_cost",
                 id="pricing-main"),
]


def model_version_variable(dashboard_name):
    dash = json.loads((REPO / "grafana" / dashboard_name).read_text(encoding="utf-8"))
    variables = [v for v in dash["templating"]["list"] if v["name"] == "model_version"]
    assert len(variables) == 1, "expected exactly one model_version variable"
    return variables[0]


@pytest.mark.parametrize("version, dashboard_name, measurement", VERSIONED)
def test_dashboard_defaults_to_the_current_model_version(version, dashboard_name,
                                                         measurement):
    assert model_version_variable(dashboard_name)["current"]["value"] == version


@pytest.mark.parametrize("version, dashboard_name, measurement", VERSIONED)
def test_current_model_version_is_a_selectable_option(version, dashboard_name,
                                                      measurement):
    variable = model_version_variable(dashboard_name)
    selected = [o for o in variable["options"] if o.get("selected")]
    assert [o["value"] for o in selected] == [version]
    assert version in variable["query"].split(",")


# Liveness panels, deliberately unfiltered. "Did the job run at all" is not a
# question about a model version: filtering would make the box go red the moment
# MODEL_VERSION is bumped, before any backfill has had a chance to run, and the
# provisioned alert that mirrors it cannot reference a dashboard variable in the
# first place. Listed rather than pattern-matched so adding one is a decision.
#
# `Nightly jobs` and `Which job is late` are the main dashboard's pair of the same
# argument, over five jobs rather than one -- and the reason to keep them unfiltered is
# sharper there: a bump would paint the verdict tile red for every job at once, which is
# the single tile most likely to be believed.
UNVERSIONED_PANELS = {"Job age", "Nightly jobs", "Which job is late"}


@pytest.mark.parametrize("version, dashboard_name, measurement", VERSIONED)
def test_every_panel_filters_on_the_variable(version, dashboard_name, measurement):
    """An unfiltered panel would sum every model version at once."""
    dash = json.loads((REPO / "grafana" / dashboard_name).read_text(encoding="utf-8"))
    unfiltered = [
        panel.get("title")
        for panel in dash.get("panels", [])
        for target in panel.get("targets", [])
        if measurement in target.get("query", "")
        and "${model_version}" not in target.get("query", "")
        and panel.get("title") not in UNVERSIONED_PANELS
    ]
    assert unfiltered == []


@pytest.mark.parametrize("version, dashboard_name, measurement", VERSIONED)
def test_the_liveness_panel_stays_unfiltered(version, dashboard_name, measurement):
    """The exemption above has to stay an exemption.

    If someone "fixes" the Job age panel by adding the version filter, the
    dashboard and the provisioned alert stop agreeing -- the alert has no
    variables to filter by -- and a version bump then reads as a dead job.
    """
    dash = json.loads((REPO / "grafana" / dashboard_name).read_text(encoding="utf-8"))
    for panel in dash.get("panels", []):
        if panel.get("title") in UNVERSIONED_PANELS:
            for target in panel.get("targets", []):
                assert "${model_version}" not in target.get("query", "")
