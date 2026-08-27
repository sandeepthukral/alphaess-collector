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
import re
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
    assert len(DASHBOARDS) == 9


def test_generators_were_found():
    """The pairing glob has the same failure mode as the dashboard glob: match nothing,
    test nothing, pass."""
    assert len(GENERATORS) == 4


@pytest.mark.parametrize("generator", GENERATORS, ids=lambda p: p.name)
def test_generated_dashboard_matches_its_generator(generator, tmp_path):
    """A generated dashboard must equal what its generator emits.

    Three of the eight are built from a script rather than exported from the Grafana UI,
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


# Panels the main dashboard shows a second copy of, keyed by the dashboard they were
# copied FROM. They are copied rather than shared because Grafana has no way to provision
# one panel into two dashboards.
#
# Both sources are generated, which is the whole risk: an edit to a generator lands in its
# own dashboard automatically and in alphaess-dashboard.json only if someone remembers.
# alphaess-dashboard.json is hand-maintained and has no generator to regenerate it, so
# nothing but this test connects the copies.
COPIED_PANELS = {
    "alphaess-battery-plan.json": [
        "Planned SoC vs actual SoC",
    ],
    "alphaess-dispatch.json": [
        "Dispatcher",
        "Decision",
        "Doing",
        "Command expires in",
        "What the dispatcher will do next",
    ],
}

# Every copied title, flat -- the range rule below applies to all of them equally.
EVERY_COPIED_PANEL = [t for ts in COPIED_PANELS.values() for t in ts]


def _overrides(panel):
    """{field: {property: value}} for the byName overrides that decide how a value reads.

    custom.* is dropped: it is layout -- column width, axis side -- and the two copies of
    a panel are allowed to be laid out differently.
    """
    out = {}
    for ov in panel.get("fieldConfig", {}).get("overrides", []):
        if ov.get("matcher", {}).get("id") != "byName":
            continue
        props = {p["id"]: p.get("value") for p in ov["properties"]
                 if not p["id"].startswith("custom.")}
        if props:
            out.setdefault(ov["matcher"]["options"], {}).update(props)
    return out


def _panels_by_title(name):
    dash = json.loads((REPO / "grafana" / name).read_text(encoding="utf-8"))
    return dash, {p.get("title"): p for p in dash["panels"]}


@pytest.mark.parametrize("source", sorted(COPIED_PANELS))
def test_copied_panels_match_their_source(source):
    """The copies on the main dashboard must run the same queries as the originals.

    Those queries are generated -- from generate-battery-plan.py and generate-dispatch.py
    -- so a change there lands in the source dashboard automatically and in
    alphaess-dashboard.json only if someone remembers. Two dashboards then disagree about
    what the battery is doing, and both look right. Mirror the change into
    grafana/alphaess-dashboard.json.

    This is what makes copying a dispatch panel onto Overview safe. The alternative
    considered was pinning the DISPATCH_LAST constant across files, which would have
    checked the query's opening lines and nothing else; comparing the whole target list
    against the generated original checks all of it, including the parts a hand-edit is
    most likely to get subtly wrong.
    """
    _, main = _panels_by_title("alphaess-dashboard.json")
    _, origin = _panels_by_title(source)

    for title in COPIED_PANELS[source]:
        assert title in main, f"{title} is missing from the main dashboard"
        assert title in origin, f"{title} is missing from {source}"
        assert [t["query"] for t in main[title]["targets"]] == \
               [t["query"] for t in origin[title]["targets"]], f"{title} ({source})"
        # Field overrides too, not just the queries: units, decimals and column names
        # decide what the numbers mean on screen, and a unit changed on one dashboard and
        # not the other is the same drift as a diverging query, and just as invisible.
        #
        # Compared per field rather than whole, and only for fields both carry: a copy on
        # the main dashboard is laid out differently -- half-width, its own column widths
        # -- and that is presentation, which is allowed to differ.
        assert _overrides(main[title]) == _overrides(origin[title]), f"{title} ({source})"


def test_the_copy_list_is_not_empty():
    """Guards the guard: an emptied list, or a source dashboard renamed, would make the
    test above iterate nothing and pass."""
    assert COPIED_PANELS
    for source, titles in COPIED_PANELS.items():
        assert (REPO / "grafana" / source).exists(), source
        assert titles, source


def test_copied_panels_span_the_plan_horizon():
    """The main dashboard's own time range must cover the plan's horizon.

    A panel time override cannot do this. `timeFrom` only ever ends at now, and Grafana
    11.6 rejects a negative `timeShift` outright -- the panel header reads "invalid
    timeshift" and the override is dropped, so the panels query `now-42h -> now` and the
    forward half of the plan is simply absent. Seen on the NAS 2026-08-08.

    So the dashboard carries the plan's range and the live panels are pinned back to 24h
    instead; test_live_panels_keep_their_own_window is the other half of this.

    It covers the dispatch tiles too, where it is not vacuous for the reason it looks like
    it might be: `What the dispatcher will do next` draws the slots AHEAD of now, so a
    timeFrom override on it would leave a panel whose entire subject is the future
    rendering a blank strip -- the same failure as 2026-08-08, on a different panel.
    """
    plan_dash, _ = _panels_by_title("alphaess-battery-plan.json")
    main_dash, main = _panels_by_title("alphaess-dashboard.json")

    assert main_dash["time"] == plan_dash["time"]

    for title in EVERY_COPIED_PANEL:
        panel = main[title]
        assert "timeFrom" not in panel and "timeShift" not in panel, (
            f"{title} must take the dashboard's range, not an override that ends at now")


def test_live_panels_keep_their_own_window():
    """The panels that show what is happening now must not stretch to the plan's horizon.

    They share a dashboard with the plan panels, whose range runs 36 hours forward. Without
    an override they would draw six hours of data against a day and a half of blank -- the
    live view spending most of its width on the future it knows nothing about.

    Only the three that read v.timeRange need it; the four stat panels range over a fixed
    -1h or -90d by design and are unaffected by the picker.
    """
    _, main = _panels_by_title("alphaess-dashboard.json")

    for title in ("Energy Flow: Sources -> Uses", "Solar vs Load vs SoC", "Power"):
        assert main[title].get("timeFrom") == "24h", title


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_panel_ids_are_unique(path):
    """Grafana keys a dashboard's panels by id, and tolerates a duplicate silently: the
    second copy loads, panel links and "view panel" URLs resolve to whichever it picked, and
    nothing anywhere says so. The failure is a link that opens the wrong chart.

    Worth a test rather than care, because the way a duplicate gets written is by reading the
    highest id off the generator and adding one -- and the generators no longer emit their
    panels in id order.
    """
    ids = [p["id"] for p in json.loads(path.read_text()).get("panels", [])]
    assert len(ids) == len(set(ids)), f"{path.name}: duplicate panel ids {sorted(ids)}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_two_panels_overlap_in_the_grid(path):
    """Two panels claiming the same cell is a layout Grafana resolves by pushing one of them
    somewhere else, so the dashboard that deploys is not the one the generator describes.

    This is the guard that makes "insert a row and shift everything below it down" a
    mechanical change rather than an eyeballed one -- that move has been made three times on
    the Battery Plan dashboard now (see its version history), each time by hand, each time
    with every y below the insertion point needing to move by exactly the right amount.
    """
    occupied: dict[tuple[int, int], int] = {}
    for panel in json.loads(path.read_text()).get("panels", []):
        g = panel["gridPos"]
        for y in range(g["y"], g["y"] + g["h"]):
            for x in range(g["x"], g["x"] + g["w"]):
                clash = occupied.get((x, y))
                assert clash is None, (
                    f"{path.name}: panels {clash} and {panel['id']} both occupy "
                    f"cell (x={x}, y={y})")
                occupied[(x, y)] = panel["id"]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_no_panel_claims_a_per_pack_temperature(path):
    """0x010B-0x0110 report the coldest and hottest cell across the WHOLE battery, tagged
    with the pack each came from -- not one reading per pack. A panel titled "Pack 2 temp"
    would be charting a fleet extreme as if it were one box's temperature, and it would look
    entirely reasonable on screen. `dispatch/registers.py` documents the same constraint at
    the source; this is the end of that contract nobody reads before adding a panel.
    """
    for panel in json.loads(path.read_text()).get("panels", []):
        assert not re.search(r"pack\s*\d+\s*temp", panel.get("title", ""), re.I), \
            f"{path.name}: {panel['title']!r} claims a per-pack temperature"


def test_the_two_app_tables_share_a_row():
    """Both tables answer the same question -- what to type into the AlphaESS app -- and read
    as one instruction, so they sit side by side rather than one scroll apart.

    `test_no_two_panels_overlap_in_the_grid` proves the layout is legal; nothing proved it was
    the intended one, and a full-width table is the shape both of these had before and the
    shape a later edit would drift back to.
    """
    _, plan = _panels_by_title("alphaess-battery-plan.json")
    left = plan["What to set in the app"]["gridPos"]
    right = plan["Planned Actions in app"]["gridPos"]
    assert left["y"] == right["y"], "the two app tables are no longer on one row"
    assert left["w"] + right["w"] == 24, "the paired row does not fill the board's width"
    assert left["x"] + left["w"] <= right["x"], "the two app tables overlap"


def test_the_temperature_history_keeps_its_own_window():
    """A fourth case of `test_live_panels_keep_their_own_window`'s rule, on the same board.

    The main dashboard carries the plan's `now-6h` to `now+36h` range (the test above pins
    that), so a temperature history drawn against it spends most of its width on a future no
    measurement can fill -- the 2026-08-08 failure in a different panel. `timeFrom` pins this
    one to seven days regardless of the picker; seven rather than the trio's 24h because a
    battery's thermal story is a week long, not a day.

    Kept separate from the trio's test rather than folded into its title list, because that
    test asserts one shared value and this panel deliberately differs from it.
    """
    _, main = _panels_by_title("alphaess-dashboard.json")
    assert main["Battery cell temperature (min/max)"].get("timeFrom") == "7d"


def test_the_min_temp_tile_keeps_a_cold_band():
    """The min tile's whole argument is that COLD is what matters there -- a lithium pack near
    freezing refuses or derates a charge, which its own description says. Sharing the max
    tile's hot-side ladder would render -20 C in the same comfortable green as 20 C, and that
    is a one-line "tidy up the duplicate thresholds" refactor away at any time.

    Stated as a behaviour rather than a step list: whatever the numbers become, a freezing
    reading must not paint the same colour as a comfortable one.
    """
    _, main = _panels_by_title("alphaess-dashboard.json")
    steps = main["Min cell temp"]["fieldConfig"]["defaults"]["thresholds"]["steps"]

    def colour_at(value):
        chosen = steps[0]["color"]
        for step in steps[1:]:
            if value >= step["value"]:
                chosen = step["color"]
        return chosen

    assert colour_at(-20.0) != colour_at(20.0), \
        f"a freezing min cell reads the same colour as a comfortable one: {steps}"
    # And the max tile is left alone: its base is the comfortable colour, because a max cell
    # can only be cold if the whole battery is, which the min tile already says louder.
    max_steps = main["Max cell temp"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert max_steps[0]["value"] is None and max_steps[0]["color"] == "green"


def _capacity_var(path):
    """The `capacity_wh` template variable of one dashboard, or None if it has none."""
    dash = json.loads(path.read_text(encoding="utf-8"))
    for var in dash.get("templating", {}).get("list", []):
        if var["name"] == "capacity_wh":
            return var
    return None


def _dashboards_with_capacity():
    return [(p, v) for p in DASHBOARDS if (v := _capacity_var(p)) is not None]


def test_the_capacity_variable_was_found_on_more_than_one_dashboard():
    """Anti-vacuity. The agreement test below is a loop; if the variable were ever renamed,
    the loop would find nothing and pass while every copy drifted freely.

    Three today: the main dashboard, the plan dashboard and the score dashboard. The last
    was missed by this test's earlier form, which named two files by hand -- despite
    carrying `27900` itself and dividing by it on thirteen panels.
    """
    found = _dashboards_with_capacity()
    assert len(found) >= 3, (
        f"only {[p.name for p, _ in found]} carry a capacity_wh variable -- either it was "
        f"renamed, or a dashboard that divides by capacity has stopped declaring it")


def test_no_dashboard_still_holds_its_own_copy_of_the_capacity():
    """The half-done state `PLAN-repo-seams.md` Part 2a exists to prevent.

    Until 2026-08-17 each dashboard carried `27900` as a textbox, and every SoC percentage
    divided by its own copy. A capacity change applied to one and not the others renders a
    plausible, wrong percentage -- no error, just a confident wrong number. The planner now
    publishes `capacity_wh` on every `plan` point, so the number travels with the data it
    explains and the dashboards read it.

    This is the earlier agreement test in its new shape: it no longer compares the copies to
    each other, it asserts there are none. A dashboard reverted to a textbox -- by an export
    from the Grafana UI, most likely -- fails here rather than drifting quietly.
    """
    literal = {p.name: v for p, v in _dashboards_with_capacity()
               if v.get("type") != "query"}
    assert not literal, (
        f"these dashboards hold a hardcoded capacity instead of reading it from the plan: "
        f"{ {n: v.get('query') for n, v in literal.items()} }")


def test_every_dashboard_reads_the_capacity_from_the_same_place():
    """Three dashboards, three copies of the query, and no shared module to put it in --
    the generators are standalone scripts and one dashboard has no generator at all.

    So the copies stay, but of a query rather than of a number, and this pins them equal.
    The distinction that matters: a stale copy of the query is visible here, whereas a stale
    copy of the number was only visible on the NAS, as a percentage nobody could check.
    """
    queries = {p.name: v["query"] for p, v in _dashboards_with_capacity()}
    assert len(set(queries.values())) == 1, (
        f"dashboards disagree about how to read the capacity: {queries}")

    query = next(iter(queries.values()))
    assert 'bucket: "planning"' in query, query
    assert '_field == "capacity_wh"' in query, query
    # int(), because every consumer interpolates this as `float(v: ${capacity_wh})` and the
    # field is a float. A value Grafana renders as 2.79e+04 is not parseable there.
    assert "int(v:" in query, query
    # group(), because `plan` is tagged with plan_run: without it the filter yields one table
    # per run and the variable is offered a list of values rather than one. Confirmed against
    # the live bucket -- the first version of this query returned 2 rows.
    assert "|> group()" in query, query
    # Sorted by plan_run, not by _time. Runs share a horizon end (the window is cut at the end
    # of the priced period, not a fixed span from the run), so "the row with the greatest
    # _time" ties across every run in flight and breaks on the day the capacity changes.
    assert 'sort(columns: ["_run"], desc: true)' in query, query
    assert "time(v: r.plan_run)" in query, query


def test_the_capacity_variable_is_configured_to_actually_re_read_the_query():
    """`type: "query"` is not enough on its own, and both of the fields below are ways a
    dashboard goes back to serving a constant while still looking correct in a diff.

    `current` is written by an export from the Grafana UI, which bakes whatever the variable
    resolved to at export time into the file. `refresh: 0` means never re-run -- the query is
    still right there in the JSON, and never executes. Either one restores exactly the
    failure this whole change removes: a confident wrong percentage, with a query above it
    that reads correctly.

    Matters most for alphaess-dashboard.json, which has no generator to regenerate it from
    and is the dashboard the dispatcher will be watched on.
    """
    for path, var in _dashboards_with_capacity():
        assert var.get("current") == {}, (
            f"{path.name} has a baked-in `current` for capacity_wh ({var.get('current')!r}) "
            f"-- almost certainly a Grafana UI export; it will serve that value forever")
        # 1 = on dashboard load. 2 would be on time-range change, which is wrong for a value
        # that cannot depend on the time range, but would at least still re-run.
        assert var.get("refresh") == 1, (
            f"{path.name} has refresh={var.get('refresh')!r} for capacity_wh -- 0 means the "
            f"query never runs and the variable is frozen at whatever `current` holds")


def test_the_generators_emit_the_same_capacity_query_as_their_dashboards():
    """The generated dashboards are committed, so a generator edited without re-running it
    leaves the two disagreeing -- and the dashboard, not the generator, is what deploys."""
    committed = {v["query"] for _, v in _dashboards_with_capacity()}
    assert len(committed) == 1
    query = committed.pop()
    found = 0
    for gen in GENERATORS:
        text = gen.read_text(encoding="utf-8")
        if "capacity_wh" not in text:
            continue
        found += 1
        assert query in text, (
            f"{gen.name} mentions capacity_wh but does not contain the query its dashboard "
            f"carries -- it has drifted from the dashboard it generates")
    assert found >= 2, (
        f"only {found} generator(s) mention capacity_wh; this loop has gone vacuous")


ALERT_FILES = sorted((PROVISIONING / "alerting").glob("*.yml"))


def test_alert_rules_were_found():
    """Same failure mode as the dashboard glob: match nothing, test nothing, pass."""
    assert len(ALERT_FILES) == 2


@pytest.mark.parametrize("path", ALERT_FILES, ids=lambda p: p.name)
def test_alert_rule_names_its_datasource(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    uids = [
        query["datasourceUid"]
        for group in doc["groups"]
        for rule in group["rules"]
        for query in rule["data"]
    ]
    assert uids, f"expected {path.name} to carry queries"
    assert all(uid == DS_UID or uid in BUILTIN_UIDS for uid in uids), uids


def test_alert_rule_uids_are_unique():
    """Grafana keys provisioned rules by uid. A duplicate silently replaces the
    other rule -- one of the two alerts simply never exists."""
    uids = [rule["uid"]
            for path in ALERT_FILES
            for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]
            for rule in group["rules"]]
    assert len(uids) == len(set(uids)), uids


def test_dashboard_provider_and_alert_folder_agree():
    """Grafana matches the folder by title, so the files must agree exactly."""
    providers = yaml.safe_load(
        (PROVISIONING / "dashboards" / "dashboards.yml").read_text(encoding="utf-8")
    )["providers"]
    folders = {p["folder"] for p in providers}
    for path in ALERT_FILES:
        folders |= {g["folder"]
                    for g in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]}
    assert folders == {"AlphaESS"}


def test_status_panel_and_staleness_alert_share_a_threshold():
    """The 'Collector status' box is the alert rendered on screen.

    A green box while the alert is firing (or the reverse) is worse than having
    neither: the dashboard is the thing looked at first when a Kuma notification
    arrives, so a disagreement sends you hunting for a second, non-existent
    fault. Both must break on the same number of seconds.
    """
    alerting = yaml.safe_load(
        (PROVISIONING / "alerting" / "alphaess-staleness.yml").read_text(encoding="utf-8")
    )["groups"]
    alert_thresholds = [
        param
        for group in alerting
        for rule in group["rules"]
        for query in rule["data"]
        for condition in query["model"].get("conditions", [])
        for param in condition["evaluator"]["params"]
    ]
    assert len(alert_thresholds) == 1, alert_thresholds

    dashboard = json.loads(
        (REPO / "grafana" / "alphaess-collector-health.json").read_text(encoding="utf-8")
    )
    panel = next(
        p for p in dashboard["panels"] if p["title"] == "Collector status"
    )
    defaults = panel["fieldConfig"]["defaults"]

    # The red threshold step, and the boundary between the ALL OK and OUTAGE
    # value mappings: both express the same rule, so both are checked.
    steps = [s["value"] for s in defaults["thresholds"]["steps"] if s["value"] is not None]
    ok = next(m for m in defaults["mappings"] if m["options"].get("result", {}).get("text") == "ALL OK")
    outage = next(m for m in defaults["mappings"] if m["options"].get("result", {}).get("text") == "OUTAGE")

    assert steps == alert_thresholds
    assert ok["options"]["to"] == alert_thresholds[0]
    assert outage["options"]["from"] == alert_thresholds[0]


def _alert_thresholds(filename):
    doc = yaml.safe_load((PROVISIONING / "alerting" / filename).read_text(encoding="utf-8"))
    return [
        param
        for group in doc["groups"]
        for rule in group["rules"]
        for query in rule["data"]
        for condition in query["model"].get("conditions", [])
        for param in condition["evaluator"]["params"]
    ]


def test_job_age_panel_and_efficiency_alert_share_a_threshold():
    """Same contract as the Collector status box above, for the nightly job.

    The 'Job age' stat is the staleness alert rendered on screen. A green box
    while the alert fires sends you hunting for a second, non-existent fault --
    and here it would do so for a job whose only symptom is that a chart stopped
    moving, which is hard enough to spot without the dashboard lying about it.
    """
    alert_thresholds = _alert_thresholds("alphaess-efficiency-staleness.yml")
    assert len(alert_thresholds) == 1, alert_thresholds

    dashboard = json.loads(
        (REPO / "grafana" / "alphaess-energy-losses.json").read_text(encoding="utf-8")
    )
    panel = next(p for p in dashboard["panels"] if p["title"] == "Job age")
    steps = [s["value"] for s in panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
             if s["value"] is not None]
    assert steps[-1] == alert_thresholds[0]


def test_the_staleness_checks_read_when_the_job_ran_not_which_day_it_wrote():
    """daily_energy rows are stamped at the local midnight of the day they
    describe, so the newest row is 51 hours old on a healthy system right before
    the next nightly run. Anything measuring staleness from the row's own
    timestamp needs a >51h threshold and takes two and a half days to notice a
    dead job -- which is why efficiency.py stores computed_at_unix at all.

    Pins that both readers actually use it.
    """
    alert = (PROVISIONING / "alerting" / "alphaess-efficiency-staleness.yml").read_text(
        encoding="utf-8")
    assert 'r._field == "computed_at_unix"' in alert

    dashboard = json.loads(
        (REPO / "grafana" / "alphaess-energy-losses.json").read_text(encoding="utf-8")
    )
    panel = next(p for p in dashboard["panels"] if p["title"] == "Job age")
    assert 'computed_at_unix' in panel["targets"][0]["query"]


def test_the_dispatch_action_table_uses_the_translator_s_own_floors():
    """The action table re-implements dispatch/translator.py:classify in Flux.

    It has to. slots.json lives on a volume inside the dispatch container, not in InfluxDB,
    so Grafana cannot read the translator's real output -- and when that container is down
    is exactly when the question "what would it do?" is worth asking.

    The cost of a second implementation is drift, and drift here is silent: the panel keeps
    rendering plausible labels while disagreeing with the dispatcher about which intervals
    hold. This pins the numbers, which is the half that can drift without anyone touching
    the panel -- lowering ENERGY_FLOOR_WH in translator.py and leaving 50.0 in the query
    would relabel intervals on the chart but not in the commands.

    The classifier's SHAPE is not pinned; keep the two in step by hand when the branches
    change, and read translator.py:classify beside the query.
    """
    from translator import ENERGY_FLOOR_WH, FULL_TOLERANCE_WH, SURPLUS_FLOOR_WH

    _, plan = _panels_by_title("alphaess-battery-plan.json")
    query = plan["What dispatch would do, interval by interval"]["targets"][0]["query"]

    for expected in (
        # classify(): charging / discharging, then the grid legs that separate a trade
        # from plain self-consumption.
        f"r.charge_wh > {ENERGY_FLOOR_WH}",
        f"r.discharge_wh > {ENERGY_FLOOR_WH}",
        f"r.export_wh > {ENERGY_FLOOR_WH}",
        f"r.import_wh > {ENERGY_FLOOR_WH}",
        # _can_harvest(): full to within a tolerance, not importing, and something to
        # harvest. Without this leg a full battery reads as a hold and the panel would
        # advertise an export that does not happen.
        f"- {FULL_TOLERANCE_WH}",
        f"r.import_wh <= {SURPLUS_FLOOR_WH}",
        f"r.export_wh > {SURPLUS_FLOOR_WH}",
        f"r.pv_forecast_wh > {SURPLUS_FLOOR_WH}",
    ):
        assert expected in query, f"{expected!r} missing -- the panel has drifted from classify()"


def test_the_dispatch_action_table_says_it_is_not_the_command():
    """slots.py:decide re-checks every target against LIVE SoC and turns a charge the
    battery has already overshot into a hold. This table cannot see that -- it reads the
    plan, which is the whole reason it works with dispatch down.

    So the panel is intent, not prediction, and the gap between them is the drift the
    dashboard exists to show. A description that omits this reads as a forecast of the
    commands, which is exactly wrong in the case that matters.
    """
    _, plan = _panels_by_title("alphaess-battery-plan.json")
    description = plan["What dispatch would do, interval by interval"]["description"]
    assert "actual SoC" in description
