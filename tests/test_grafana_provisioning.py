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

# Generated dashboards, paired with their script by filename: generate-X.py emits
# alphaess-X.json. Deriving the pairing rather than listing it means a new generated
# dashboard is covered the moment it is added, instead of whenever someone remembers to
# extend this file -- which is the same class of omission the mount test exists to catch.
GENERATORS = sorted((REPO / "grafana").glob("generate-*.py"))

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
    assert len(DASHBOARDS) == 6


def test_generators_were_found():
    """The pairing glob has the same failure mode as the dashboard glob: match nothing,
    test nothing, pass."""
    assert len(GENERATORS) == 2


@pytest.mark.parametrize("generator", GENERATORS, ids=lambda p: p.name)
def test_generated_dashboard_matches_its_generator(generator, tmp_path):
    """A generated dashboard must equal what its generator emits.

    Two of the six are built from a script rather than exported from the Grafana UI,
    because their Flux queries were written and checked against the live database and are
    the substance of the dashboard. That only holds while the two agree. Hand-edit the
    JSON and the next regeneration silently reverts it; hand-edit it and never regenerate,
    and the script becomes a lie that the next person edits instead.

    Nobody reviews generated JSON, which is exactly how a wrong query gets in: it renders
    as a plausible chart, not as an error.
    """
    name = generator.name.replace("generate-", "alphaess-").replace(".py", ".json")
    committed = REPO / "grafana" / name
    assert committed.exists(), f"{generator.name} emits {name}, which is not committed"
    out = tmp_path / "regenerated.json"
    subprocess.run([sys.executable, str(generator), str(out)], check=True,
                   capture_output=True)
    assert out.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8"), (
        f"grafana/{name} is out of sync with its generator. Edit "
        f"grafana/{generator.name}, then re-run it:\n"
        f"  python grafana/{generator.name} grafana/{name}"
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


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_series_sharing_an_axis_agree_on_its_scale(path):
    """Series pushed to the same side of a panel must agree on unit and axis label.

    Grafana derives a series' y-scale key from both, so two series with the same unit but
    a different `custom.axisLabel` get two independently auto-ranged axes on that side.
    Nothing errors: the panel grows a second column of numbers, and the two lines then sit
    at unrelated heights -- worst on exactly the panels built to compare them.

    Seen on the NAS 2026-08-02, where the battery plan's `market price` carried the label
    and the `sell above`/`buy below` threshold lines did not, so the dashed lines sat about
    23 percentage points of panel height above where the price scale would have put them.
    """
    dash = json.loads(path.read_text(encoding="utf-8"))
    disagreements = []

    for panel in dash.get("panels", []):
        # {placement: {series name: (unit, axis label)}}
        by_side = {}
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            matcher = override.get("matcher", {})
            if matcher.get("id") != "byName":
                continue
            props = {p["id"]: p.get("value") for p in override.get("properties", [])}
            placement = props.get("custom.axisPlacement")
            if placement not in ("left", "right"):
                continue
            by_side.setdefault(placement, {})[matcher.get("options")] = (
                props.get("unit"), props.get("custom.axisLabel"))

        for placement, series in by_side.items():
            if len(set(series.values())) > 1:
                disagreements.append((panel.get("title"), placement, series))

    assert disagreements == []


def test_price_line_reads_the_plan_ahead_of_now():
    """Forward of now the price must come from the plan, not from the collector's feed.

    The collector's `market_price` is refreshed every three hours from a feed that publishes
    tomorrow later than the auction the planner reads, so on that source alone the panel spends
    hours a day drawing bars and thresholds over an empty tomorrow. The two ranges also have to
    stay disjoint at now(), or union() emits two values per timestamp where they overlap.
    """
    dash = json.loads(
        (REPO / "grafana" / "alphaess-battery-plan.json").read_text(encoding="utf-8"))
    panels = [p for p in dash["panels"]
              if p.get("title") == "Planned charge / discharge, against price"]
    assert len(panels) == 1, [p.get("title") for p in dash["panels"]]

    price = [t["query"] for t in panels[0]["targets"] if '"market price"' in t.get("query", "")]
    assert len(price) == 1, "expected exactly one query to emit the `market price` series"
    query = price[0]

    assert '_field == "price_market"' in query, "forward end does not read the plan's price"
    assert 'range(start: now(), stop: v.timeRangeStop)' in query
    assert 'range(start: v.timeRangeStart, stop: now())' in query, (
        "the collector's stored price must be bounded at now(), or the two sources overlap")


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
