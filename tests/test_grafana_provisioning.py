"""Grafana provisioning must stay safe to share with another project.

This Grafana is shared infrastructure: other compose projects on the same host
provision into it (DEPLOY.md, "Sharing the stack with another project"). Grafana
resolves provisioning collisions silently -- it drops a provider, overwrites a
dashboard, or picks one of two definitions by file load order -- so the failure
is a panel charting the wrong database, with no log line anywhere.

These tests pin the two rules that keep that from happening: no datasource
claims `isDefault`, and no panel or alert query relies on the default.
"""

import json
import pathlib
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
PROVISIONING = REPO / "grafana" / "provisioning"
DASHBOARDS = sorted((REPO / "grafana").glob("*.json"))

# The datasource variable every dashboard interpolates, and the uid the Grafana
# entrypoint substitutes it with.
DS_VAR = "${DS_ALPHAESS}"
DS_UID = "alphaess"

# Panels whose datasource is one of Grafana's built-ins rather than InfluxDB.
BUILTIN_UIDS = {"-- Grafana --", "-- Mixed --", "-- Dashboard --", "__expr__"}


def test_dashboards_were_found():
    """Guard against the glob silently matching nothing.

    The count is deliberate rather than `> 0`: it also catches a dashboard
    added to grafana/ but never mounted in docker-compose.yml, which provisions
    nothing and fails silently -- see test_every_dashboard_is_mounted below.
    """
    assert len(DASHBOARDS) == 5


def test_battery_plan_json_matches_its_generator(tmp_path):
    """alphaess-battery-plan.json is generated, so it must equal what the generator emits.

    It is the one dashboard here built from a script rather than exported from the
    Grafana UI, because its Flux queries were written and checked against the live
    database. That only holds while the two agree. Hand-edit the 850-line JSON and the
    next regeneration silently reverts it; hand-edit it and never regenerate, and the
    script becomes a lie that the next person edits instead.

    Nobody reviews generated JSON, which is exactly how a wrong query gets in: it renders
    as a plausible chart, not as an error.
    """
    generator = REPO / "grafana" / "generate-battery-plan.py"
    committed = REPO / "grafana" / "alphaess-battery-plan.json"
    out = tmp_path / "regenerated.json"
    subprocess.run([sys.executable, str(generator), str(out)], check=True,
                   capture_output=True)
    assert out.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8"), (
        "grafana/alphaess-battery-plan.json is out of sync with its generator. Edit "
        "grafana/generate-battery-plan.py, then re-run it:\n"
        "  python grafana/generate-battery-plan.py grafana/alphaess-battery-plan.json"
    )


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_dashboard_is_mounted(path):
    """A dashboard Grafana never sees is the quietest failure of the set.

    The entrypoint globs /etc/grafana/dashboard-src, which is populated one
    bind mount at a time. Adding grafana/<name>.json without the matching line
    in docker-compose.yml provisions nothing at all: no error, no log line, and
    the dashboard simply is not in the list.
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    mount = f"./grafana/{path.name}:/etc/grafana/dashboard-src/{path.name}:ro"
    assert mount in compose, f"{path.name} is not mounted into Grafana"


@pytest.mark.parametrize("path", sorted(PROVISIONING.glob("datasources/*.yml")),
                         ids=lambda p: p.name)
def test_no_datasource_claims_isdefault(path):
    """Two datasources claiming the default is the collision with no symptom.

    Grafana picks one, and any panel relying on "default" instead of naming its
    datasource then reads the other project's bucket and shows plausible,
    wrong numbers.
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    flagged = [ds["name"] for ds in doc.get("datasources", []) if ds.get("isDefault")]
    assert flagged == []


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_query_names_its_datasource(path):
    """Removing isDefault only stays safe while nothing resolves "default"."""
    dash = json.loads(path.read_text(encoding="utf-8"))
    unnamed = []

    def check(node, where):
        if isinstance(node, dict):
            # Only nodes that actually query need a datasource; rows and other
            # layout-only panels have no targets.
            if node.get("targets"):
                ds = node.get("datasource")
                uid = ds.get("uid") if isinstance(ds, dict) else ds
                if uid not in (DS_VAR, DS_UID) and uid not in BUILTIN_UIDS:
                    unnamed.append((where, node.get("title"), uid))
            for key, value in node.items():
                check(value, f"{where}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                check(value, f"{where}[{i}]")

    check(dash, "")
    assert unnamed == []


def test_alert_rule_names_its_datasource():
    path = PROVISIONING / "alerting" / "alphaess-staleness.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    uids = [
        query["datasourceUid"]
        for group in doc["groups"]
        for rule in group["rules"]
        for query in rule["data"]
    ]
    assert uids, "expected the staleness rule to carry queries"
    assert all(uid == DS_UID or uid in BUILTIN_UIDS for uid in uids), uids


def test_dashboard_provider_and_alert_folder_agree():
    """Grafana matches the folder by title, so the two files must agree exactly."""
    providers = yaml.safe_load(
        (PROVISIONING / "dashboards" / "dashboards.yml").read_text(encoding="utf-8")
    )["providers"]
    alerting = yaml.safe_load(
        (PROVISIONING / "alerting" / "alphaess-staleness.yml").read_text(encoding="utf-8")
    )["groups"]

    folders = {p["folder"] for p in providers} | {g["folder"] for g in alerting}
    assert folders == {"AlphaESS"}
