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

# The log store, provisioned beside InfluxDB. Its dashboard and alert live in this repo
# while the loki/alloy services live in nas-observability; see the NAS section at the
# bottom of this file.
LOKI_UID = "loki"


def test_dashboards_were_found():
    """Guard against the glob silently matching nothing.

    The count is deliberate rather than `> 0`: it also catches a dashboard
    added to grafana/ but never mounted in docker-compose.yml, which provisions
    nothing and fails silently -- see test_every_dashboard_is_mounted below.
    """
    assert len(DASHBOARDS) == 10


def test_generators_were_found():
    """The pairing glob has the same failure mode as the dashboard glob: match nothing,
    test nothing, pass."""
    assert len(GENERATORS) == 5


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


# Panels one dashboard shows a second copy of, keyed by the dashboard they were copied FROM,
# each mapped to (the dashboard they were copied INTO, the titles copied). They are copied
# rather than shared because Grafana has no way to provision one panel into two dashboards.
#
# The first two entries copy FROM a generated dashboard INTO alphaess-dashboard.json, which is
# hand-maintained and has no generator to regenerate it -- an edit to `generate-battery-plan.py`
# or `generate-dispatch.py` lands in its own dashboard automatically and in
# alphaess-dashboard.json only if someone remembers, so nothing but this test connects them.
#
# The third entry runs the other way: alphaess-dashboard.json is the hand-maintained SOURCE and
# alphaess-battery-health.json (generated) is the copy. It is the first entry in either
# direction, because it is the first panel that started life on a hand-maintained dashboard
# before a generated one wanted a copy of it.
COPIED_PANELS = {
    "alphaess-battery-plan.json": ("alphaess-dashboard.json", [
        "Planned SoC vs actual SoC",
        "Plan age",
        "Planned benefit over horizon",
    ]),
    "alphaess-dispatch.json": ("alphaess-dashboard.json", [
        "Dispatcher",
        "Decision",
        "Doing",
        "Command expires in",
        "Why",
        "What the dispatcher will do next",
    ]),
    "alphaess-battery-health.json": ("alphaess-dashboard.json", [
        "Active faults",
        "Active warnings",
        "State of health",
    ]),
    "alphaess-battery-savings.json": ("alphaess-dashboard.json", [
        "Total saving to date",
    ]),
}

# Panels the main dashboard shows a version of that is NOT a copy: same idea, different
# window. They are deliberately retitled with the window in the title, because the copy
# rule above is what makes a shared title mean "these two run the same query", and a panel
# that quietly answered a different question under the same name would break that promise
# for every other entry.
#
# The reason they cannot be copies: this dashboard's range runs 36 hours into the FUTURE
# for the plan panels, so a count over `v.timeRange` -- which is what Collector Health asks
# -- is not a number about anything here.
RESCOPED_PANELS = {
    "Failed polls (24h)": ("alphaess-collector-health.json", "Failed polls"),
    "Heartbeat pushes failing (24h)": ("alphaess-collector-health.json",
                                       "Heartbeat unreachable"),
}

# Every (destination, title) pair, flat -- the horizon rule below applies only to the pairs
# landing on alphaess-dashboard.json; see test_copied_panels_span_the_plan_horizon.
EVERY_COPIED_PANEL = [(target, t) for target, ts in COPIED_PANELS.values() for t in ts]


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
    """Rows are excluded: a row is a heading, never a thing with a query, and every test
    here looks up panels that have one. Naming a row after the tile that summarises the
    section is the obvious thing to do -- "Battery" above the battery detail -- and it
    should not have to be avoided to keep this lookup honest."""
    dash = json.loads((REPO / "grafana" / name).read_text(encoding="utf-8"))
    return dash, {p.get("title"): p for p in dash["panels"] if p.get("type") != "row"}


@pytest.mark.parametrize("source", sorted(COPIED_PANELS))
def test_copied_panels_match_their_source(source):
    """The copies must run the same queries as the originals, whichever direction the copy
    runs.

    Two of these three copy FROM a generated dashboard, so a change there lands in the
    source automatically and in the hand-maintained copy only if someone remembers. The
    third runs the other way -- alphaess-dashboard.json is hand-maintained and has no
    generator to regenerate it, so an edit made directly there is exactly as invisible to
    its generated copy. Either way, two dashboards then disagree about what the battery is
    doing, and both look right.

    This is what makes copying a panel between dashboards safe. The alternative considered
    was pinning the query constant across files, which would have checked the query's
    opening lines and nothing else; comparing the whole target list against the original
    checks all of it, including the parts a hand-edit is most likely to get subtly wrong.
    """
    target_name, titles = COPIED_PANELS[source]
    _, main = _panels_by_title(target_name)
    _, origin = _panels_by_title(source)

    for title in titles:
        assert title in main, f"{title} is missing from {target_name}"
        assert title in origin, f"{title} is missing from {source}"
        assert [t["query"] for t in main[title]["targets"]] == \
               [t["query"] for t in origin[title]["targets"]], f"{title} ({source})"
        # Field overrides too, not just the queries: units, decimals and column names
        # decide what the numbers mean on screen, and a unit changed on one dashboard and
        # not the other is the same drift as a diverging query, and just as invisible.
        #
        # Compared per field rather than whole, and only for fields both carry: a copy is
        # laid out differently -- half-width, its own column widths -- and that is
        # presentation, which is allowed to differ.
        assert _overrides(main[title]) == _overrides(origin[title]), f"{title} ({source})"


@pytest.mark.parametrize("title", sorted(RESCOPED_PANELS))
def test_rescoped_panels_count_the_same_series_over_a_fixed_window(title):
    """A re-scoped panel may change its WINDOW and nothing else.

    The two things it must not change are the series it counts -- both of these select on
    string literals, where a typo matches nothing, counts zero and reads as perfect health
    -- and its independence from the picker. Between them that is the whole panel: a fixed
    window over the right series, or a tile that is confidently green for the wrong reason.
    """
    source_name, source_title = RESCOPED_PANELS[title]
    _, main = _panels_by_title("alphaess-dashboard.json")
    _, source = _panels_by_title(source_name)

    assert title in main, f"{title} is missing from alphaess-dashboard.json"
    query = main[title]["targets"][0]["query"]
    origin = source[source_title]["targets"][0]["query"]

    assert "v.timeRange" not in query, (
        f"{title} reads the time picker, which on this dashboard runs 36h into the future")
    assert "range(start: -24h)" in query, title
    for literal in re.findall(r'(?:_measurement|\.event|_field) == "[^"]+"', origin):
        assert literal in query, f"{title} no longer selects {literal!r}, but {source_title} does"


def test_the_copy_list_is_not_empty():
    """Guards the guard: an emptied list, or a source/target dashboard renamed, would make
    the test above iterate nothing and pass."""
    assert COPIED_PANELS
    for source, (target_name, titles) in COPIED_PANELS.items():
        assert (REPO / "grafana" / source).exists(), source
        assert (REPO / "grafana" / target_name).exists(), target_name
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

    Scoped to copies landing on alphaess-dashboard.json only, which is every entry in the
    list today; the scoping stays because a copy onto any other board answers to that
    board's range, not the plan's.
    """
    plan_dash, _ = _panels_by_title("alphaess-battery-plan.json")
    main_dash, main = _panels_by_title("alphaess-dashboard.json")

    assert main_dash["time"] == plan_dash["time"]

    for target_name, title in EVERY_COPIED_PANEL:
        if target_name != "alphaess-dashboard.json":
            continue
        panel = main[title]
        assert "timeFrom" not in panel and "timeShift" not in panel, (
            f"{title} must take the dashboard's range, not an override that ends at now")


def test_live_panels_keep_their_own_window():
    """The panels that show what is happening now must not stretch to the plan's horizon.

    They share a dashboard with the plan panels, whose range runs 36 hours forward. Without
    an override they would draw six hours of data against a day and a half of blank -- the
    live view spending most of its width on the future it knows nothing about.

    One panel left where there were three. "Energy Flow: Sources -> Uses" and "Solar vs
    Load vs SoC" were dropped when the main dashboard became a status board: both are still
    on the Energy dashboard, which is what the picker exists for, and both were duplicated
    there in full. "Power" stayed because a status board that cannot say what the house is
    doing right now sends you to a second dashboard to answer the easiest question on it.

    Every other panel here ranges over a fixed window by design -- -5m, -3h, -24h, -90d --
    and is unaffected by the picker, which is the rule for anything that reads as a verdict.
    """
    _, main = _panels_by_title("alphaess-dashboard.json")

    for title in ("Power",):
        assert main[title].get("timeFrom") == "24h", title

    # The two that left must not come back by a Grafana UI export, which would restore them
    # without the override and draw them across the plan's empty future.
    for title in ("Energy Flow: Sources -> Uses", "Solar vs Load vs SoC"):
        assert title not in main, (
            f"{title} is back on the main dashboard; it belongs on Energy / Energy Flow")


# Dashboards the title-keyed tests above look panels up on. Uniqueness matters on exactly
# these: _panels_by_title builds a dict, so a second panel with the same title SHADOWS the
# first and the test then checks the wrong panel -- or passes because a row happened to
# land on the name. Caught while adding the status rows, where a row titled "Battery" sat
# above a stat titled "Battery" and the row won. That pair is deliberate now and allowed:
# _panels_by_title skips rows, so the collision it was shadowing cannot happen. Rows are
# checked against each other, because two sections with one name is its own confusion.
#
# Not applied to every dashboard: alphaess-collector-health.json deliberately carries a
# stat and a table both called "Outages", nothing looks either up by title, and renaming
# one to satisfy a test nobody needed would be the tail wagging the dog.
TITLE_KEYED = [
    "alphaess-dashboard.json",
    "alphaess-battery-plan.json",
    "alphaess-battery-health.json",
    "alphaess-battery-savings.json",
    "alphaess-dispatch.json",
]


@pytest.mark.parametrize("name", TITLE_KEYED)
def test_panel_titles_are_unique_where_tests_key_on_them(name):
    dash = json.loads((REPO / "grafana" / name).read_text(encoding="utf-8"))
    for kind in ("panel", "row"):
        titles = [p.get("title") for p in dash["panels"]
                  if (p.get("type") == "row") == (kind == "row")]
        duplicates = sorted({t for t in titles if titles.count(t) > 1})
        assert duplicates == [], f"{name}: duplicate {kind} titles {duplicates}"


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
    """Battery Health runs a plain `now-30d` to `now` lookback, so without an override the
    temperature history would spend most of its width on three weeks the thermal story does
    not need. `timeFrom` pins it to seven days regardless of the picker -- seven rather than
    the live panels' 24h because a battery's thermal story is a week long, not a day.

    Battery Health is now the only board carrying this panel. The main dashboard had a copy
    until the Battery section became one row of tiles: a week-long history is a question you
    go somewhere to ask, not a thing a status board answers, and the tiles above it already
    say whether the pack is hot or cold right now.
    """
    _, health = _panels_by_title("alphaess-battery-health.json")
    panel = health["Battery cell temperature (min/max)"]
    assert panel.get("timeFrom") == "7d"
    assert "timeShift" not in panel, (
        "a negative timeShift is silently dropped by Grafana 11.6 rather than erroring, "
        "so this has to be timeFrom, not timeShift -- see test_copied_panels_span_the_plan_"
        "horizon's docstring")


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
    assert len(ALERT_FILES) == 4


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
    # `loki` alongside `alphaess`: the container-log rule queries the log store rather
    # than InfluxDB. Both are named explicitly, which is the property this test exists
    # for -- neither is allowed to be the default.
    assert all(uid in (DS_UID, LOKI_UID) or uid in BUILTIN_UIDS for uid in uids), uids


def test_alert_rule_uids_are_unique():
    """Grafana keys provisioned rules by uid. A duplicate silently replaces the
    other rule -- one of the two alerts simply never exists."""
    uids = [rule["uid"]
            for path in ALERT_FILES
            for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]
            for rule in group["rules"]]
    assert len(uids) == len(set(uids)), uids


def test_dashboard_provider_and_alert_folder_agree():
    """Grafana matches the folder by title, so the files must agree exactly.

    Two folders now: AlphaESS for this project, NAS for the host-wide container logs,
    which cover every project on the machine and would be misfiled under AlphaESS. The
    set is pinned rather than counted so a third folder appearing by typo -- "Nas",
    "NAS " -- fails here instead of quietly becoming a fourth folder in the UI.
    """
    providers = yaml.safe_load(
        (PROVISIONING / "dashboards" / "dashboards.yml").read_text(encoding="utf-8")
    )["providers"]
    folders = {p["folder"] for p in providers}
    for path in ALERT_FILES:
        folders |= {g["folder"]
                    for g in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]}
    assert folders == {"AlphaESS", "NAS"}


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


def test_heartbeat_tile_and_alert_read_the_same_series():
    """The "Heartbeat unreachable" tile is the alert rendered on screen, same convention as
    `test_status_panel_and_staleness_alert_share_a_threshold` above.

    Sharper here than usual, because both sides select on STRING LITERALS. A typo in
    `event == "heartbeat_failed"` or in `_field == "error"` matches nothing, counts zero, and
    reads as perfect health -- on either side, silently, forever. That is precisely the
    failure class this pair was added to close, so it must not be reproducible inside the
    monitoring itself.
    """
    def selectors(flux):
        return {
            "measurement": set(re.findall(r'_measurement == "([^"]+)"', flux)),
            "event": set(re.findall(r'\.event == "([^"]+)"', flux)),
            "field": set(re.findall(r'_field == "([^"]+)"', flux)),
        }

    # Both tiles: Collector Health's, and the main dashboard's own 24h copy of the
    # question. Two places to make the same silent typo, so both are pinned.
    tiles = []
    for name, title in (("alphaess-collector-health.json", "Heartbeat unreachable"),
                        ("alphaess-dashboard.json", "Heartbeat pushes failing (24h)")):
        dashboard = json.loads((REPO / "grafana" / name).read_text(encoding="utf-8"))
        panel = next(p for p in dashboard["panels"] if p["title"] == title)
        assert len(panel["targets"]) == 1, panel["targets"]
        tiles.append(selectors(panel["targets"][0]["query"]))
    assert tiles[0] == tiles[1], "the two heartbeat tiles count different points"
    tile = tiles[0]

    doc = yaml.safe_load(
        (PROVISIONING / "alerting" / "alphaess-heartbeat-unreachable.yml")
        .read_text(encoding="utf-8"))
    queries = [q["model"]["query"]
               for group in doc["groups"] for rule in group["rules"]
               for q in rule["data"] if "query" in q["model"]]
    assert len(queries) == 1, queries
    alert = selectors(queries[0])

    assert tile == alert, "the tile and the alert must count the same points"
    # And that they agree on the WRONG series is the other way this can fail, so name the
    # values `collector.write_health_event` is actually called with.
    assert tile == {"measurement": {"collector_health"},
                    "event": {"heartbeat_failed"},
                    "field": {"error"}}


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


def _nightly_jobs_queries():
    _, panels = _panels_by_title("alphaess-dashboard.json")
    return {t: panels[t]["targets"][0]["query"] for t in ("Nightly jobs", "Which job is late")}


def test_the_nightly_jobs_allowance_matches_the_efficiency_alert():
    """A third copy of the same 30 hours, in a third unit.

    The alert holds it as 108000 seconds, the Job age stat as a threshold in seconds, and
    the Overview's nightly-jobs query as `- 30.0` hours subtracted from an age. Nothing at
    runtime couples them, and a drift is invisible in the worst direction: the Overview
    would read ALL FRESH for the hours the alert was already firing, on the one board whose
    entire promise is that a dead job shows up on it.
    """
    (alert_seconds,) = _alert_thresholds("alphaess-efficiency-staleness.yml")
    hours = alert_seconds / 3600.0
    for title, query in _nightly_jobs_queries().items():
        for job in ("pricing", "efficiency", "mijnbatterij"):
            assert f'job: "{job}", _time' in query, f"{title}: {job} row is gone"
        assert query.count(f"- {hours}") == 3, (
            f"{title}: expected pricing, efficiency and mijnbatterij to allow "
            f"{hours}h, matching the efficiency alert's {alert_seconds}s")


def test_the_prices_row_asks_for_coverage_not_an_age():
    """The prices job is the one whose freshness is not a staleness question.

    Day-ahead publishes early afternoon for the following day, so "how old is the newest
    price" stays comfortably negative all evening while the planner schedules tomorrow's
    charge on prices that were never fetched -- the 2026-09-03 failure, where a DSM task
    fired once before publication and its repeat window was zero minutes wide. The row has
    to ask what is HELD, against the end of a local day, or it cannot see that at all.

    Pinned because reverting it to an age looks like a simplification: every other row in
    the query is an age, and this one would read as the odd one out.
    """
    for title, query in _nightly_jobs_queries().items():
        assert 'timezone.location(name: "Europe/Amsterdam")' in query, (
            f"{title}: the day boundary is local -- UTC would shift the cutoff by an hour "
            "in summer and move it across midnight")
        assert "date.hour(t: now()) >= 15" in query, f"{title}: publication grace is gone"
        assert "then 47.0 else 23.0" in query, f"{title}: day-coverage requirement changed"
        assert "neededS - float(v: int(v: r._time))" in query, (
            f"{title}: prices is measured against now again, not against the day it must "
            "cover")


def test_every_job_has_a_fallback_row():
    """A job with no rows at all must still appear, or the verdict is a lie.

    union() drops an empty table silently, so a job that has never written -- or died
    before the query's lookback -- would simply not be in the max(), and the tile would
    read ALL FRESH with a job missing entirely. That is the exact failure the panel was
    added to catch, so it is pinned rather than left to the comment beside it.
    """
    for title, query in _nightly_jobs_queries().items():
        for job in ("prices", "pricing", "efficiency", "mijnbatterij", "plan score"):
            assert f'{{job: "{job}", _time: now(), _value: 9999.0}}' in query, \
                f"{title}: {job} has no fallback row"
        # And the reduction that makes the fallback lose to a real row: without the
        # per-job min(), every job would read 9999 and the tile would be permanently red.
        assert 'group(columns: ["job"])\n  |> min()' in query, title


def test_the_two_nightly_jobs_panels_share_one_body():
    """The verdict tile and the table run the same 60-line union, deliberately: Grafana has
    no way to share a query between two panels, and the alternative -- one table doing both
    jobs -- gives up the single-glance verdict that is the point of the header row.

    What that costs is the risk of editing one and not the other, which would leave the
    tile green while the table listed a late job. So the shared half is pinned identical
    and only the final reduction is allowed to differ.
    """
    tile, table = (_nightly_jobs_queries()[t] for t in ("Nightly jobs", "Which job is late"))
    marker = "jobs\n  |>"
    assert tile.split(marker)[0] == table.split(marker)[0]
    assert "|> max()" in tile and "|> sort(" in table


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


def test_round_trip_efficiency_carries_the_same_soc_correction_as_the_loss():
    """efficiency.py stores battery_loss_kwh as charge - discharge - dSoC, and the
    round-trip tile beside it has to divide by the same corrected quantity.

    Uncorrected, discharge/charge is an efficiency only over a window that starts and ends
    at the same SoC. Over any other window it reads the drift: a fortnight ending 14 kWh
    down printed 101.31% next to a battery-loss tile that had already subtracted that same
    14 kWh -- two tiles disagreeing about whether the SoC term exists, on the panel whose
    stated job is to be the sanity check on the pipeline. Above 100% it does not read as a
    range artefact, it reads as broken data, and there is no way to tell from the board
    which it is.

    Pins the correction, and pins that >100% is coloured as the fault it now would be.
    """
    _, panels = _panels_by_title("alphaess-energy-losses.json")
    query = panels["Round-trip efficiency"]["targets"][0]["query"]
    assert "delta_soc_kwh" in query, "round-trip is back to the uncorrected ratio"
    assert "charge - dsoc" in query, "the denominator no longer nets off delta SoC"

    steps = panels["Round-trip efficiency"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps[-1] == {"color": "red", "value": 100}, (
        "corrected, an efficiency above 100% is a data fault and must not read green")


def test_the_totals_say_when_the_range_is_missing_days():
    """A day that fails the gate in efficiency.py is not written at all, and every sum on
    this board renders identically whether the range holds fourteen rows or eleven.

    So the board needs to count the hole itself -- from the picker, not from the stored
    rows, since the thing being caught is that nothing was stored.
    """
    _, panels = _panels_by_title("alphaess-energy-losses.json")
    query = panels["Days missing"]["targets"][0]["query"]
    assert "v.timeRangeStop" in query and "v.timeRangeStart" in query, (
        "expected days must come from the picker, not from a count of what was stored")
    assert "count()" in query

    steps = panels["Days missing"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps[0]["color"] == "green" and steps[1] == {"color": "orange", "value": 1}, (
        "a single missing day has to leave the green state -- that is the whole point")


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


# --------------------------------------------------------------------------------------
# NAS-wide container logs (Loki).
#
# The `loki` and `alloy` services live in the nas-observability repo; the datasource, the
# dashboard and the alert rule live here, because Grafana provisioning cannot be owned by
# two repos at once. That split is exactly why these need tests: nothing in either repo
# fails loudly when the two halves disagree.
#
# grafana/nas/nas-logs.json is deliberately in a SUBDIRECTORY so the DASHBOARDS glob above
# does not pick it up -- it queries Loki, not InfluxDB, so every rule in this file about
# ${DS_ALPHAESS} and Flux is wrong for it. It gets its own checks below instead.
# --------------------------------------------------------------------------------------

NAS_DASHBOARD = REPO / "grafana" / "nas" / "nas-logs.json"
NAS_ALERT = PROVISIONING / "alerting" / "nas-log-errors.yml"

# Two things set the level and they disagree on case: Alloy parses the Python services
# explicitly and keeps Python's spelling (WARNING, ERROR), while Loki's own
# discover_log_levels heuristic classifies everything else in lowercase (warn, error).
CASE_INSENSITIVE = "(?i)"


def test_the_nas_dashboard_is_not_in_the_alphaess_glob():
    """Guards the arrangement the rest of these tests depend on.

    Moved up to grafana/, nas-logs.json joins DASHBOARDS and fails half the file at once:
    it names `uid: loki` rather than ${DS_ALPHAESS}, and it is mounted to a different path
    than dashboard-src. The failures would be real but would read as bugs in the dashboard
    rather than as it being in the wrong folder.
    """
    assert NAS_DASHBOARD.exists()
    assert NAS_DASHBOARD not in DASHBOARDS


def test_the_loki_datasource_is_provisioned_and_not_default():
    """The dashboard and the alert rule both name `uid: loki`; nothing else creates it.

    isDefault is checked for every datasource by test_no_datasource_claims_isdefault, but
    it is worth naming the consequence for this one specifically: a log store winning the
    default would make every InfluxDB panel that ever relies on it query logs, and Grafana
    would report that as an empty panel rather than an error.
    """
    doc = yaml.safe_load((PROVISIONING / "datasources" / "loki.yml").read_text("utf-8"))
    (ds,) = doc["datasources"]
    assert ds["uid"] == LOKI_UID
    assert ds["type"] == "loki"
    assert not ds.get("isDefault")
    # The service name on alphaess-net, not the NAS's IP -- the same rule DEPLOY.md,
    # "Sharing the stack" states for INFLUX_URL.
    assert ds["url"] == "http://loki:3100"


def test_the_nas_dashboard_is_mounted_at_its_own_path():
    """Same failure as test_every_dashboard_is_mounted, different path.

    This one cannot share that test: dashboard-src exists only so the entrypoint's sed can
    substitute ${DS_ALPHAESS}, and a Loki dashboard put through it would be provisioned
    into the AlphaESS folder by the wrong provider.
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    mount = "./grafana/nas/nas-logs.json:/etc/grafana/dashboards-nas/nas-logs.json:ro"
    assert mount in compose, "nas-logs.json is not mounted into Grafana"


def test_the_nas_provider_and_alert_agree_on_the_folder():
    """Grafana matches a provisioned folder by title, so a typo puts the dashboard and the
    alert that links to it in two different folders -- both present, neither wrong-looking.

    The same coupling the AlphaESS provider's own comment warns about, one folder along.
    """
    providers = yaml.safe_load(
        (PROVISIONING / "dashboards" / "dashboards.yml").read_text("utf-8"))
    nas = [p for p in providers["providers"] if p["name"] == "nas"]
    assert len(nas) == 1, [p["name"] for p in providers["providers"]]
    assert nas[0]["options"]["path"] == "/etc/grafana/dashboards-nas"

    alert = yaml.safe_load(NAS_ALERT.read_text("utf-8"))
    assert alert["groups"][0]["folder"] == nas[0]["folder"] == "NAS"


def test_every_nas_panel_names_the_loki_datasource():
    """The default-datasource rule the AlphaESS dashboards are held to, applied here.

    Worth restating rather than assuming: this dashboard was written by hand against a
    datasource that did not exist yet, which is the situation in which a panel most easily
    ends up with none.
    """
    dash = json.loads(NAS_DASHBOARD.read_text(encoding="utf-8"))
    unnamed = []
    for panel in dash["panels"]:
        for node in [panel, *panel.get("targets", [])]:
            ds = node.get("datasource")
            uid = ds.get("uid") if isinstance(ds, dict) else ds
            if uid not in (LOKI_UID, *BUILTIN_UIDS):
                unnamed.append((panel.get("title"), uid))
    assert unnamed == []


def test_nas_panel_ids_are_unique():
    """Grafana tolerates a duplicate panel id silently; see test_panel_ids_are_unique."""
    ids = [p["id"] for p in json.loads(NAS_DASHBOARD.read_text())["panels"]]
    assert len(ids) == len(set(ids)), f"duplicate panel ids {sorted(ids)}"


def test_no_two_nas_panels_overlap_in_the_grid():
    """Two panels claiming a cell is resolved by Grafana moving one, so what deploys is not
    what the file describes. Same check as the AlphaESS dashboards get."""
    occupied: dict[tuple[int, int], int] = {}
    for panel in json.loads(NAS_DASHBOARD.read_text())["panels"]:
        g = panel["gridPos"]
        for y in range(g["y"], g["y"] + g["h"]):
            for x in range(g["x"], g["x"] + g["w"]):
                clash = occupied.get((x, y))
                assert clash is None, (
                    f"panels {clash} and {panel['id']} both occupy cell (x={x}, y={y})")
                occupied[(x, y)] = panel["id"]


def _detected_level_queries():
    """Every query in this repo that filters on log severity, as (where, query) pairs."""
    found = []
    dash = json.loads(NAS_DASHBOARD.read_text(encoding="utf-8"))
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            if "detected_level" in target.get("expr", ""):
                found.append((f"nas-logs.json/{panel['title']}", target["expr"]))
    alert = yaml.safe_load(NAS_ALERT.read_text("utf-8"))
    for rule in alert["groups"][0]["rules"]:
        for query in rule["data"]:
            expr = query.get("model", {}).get("expr", "")
            if "detected_level" in expr:
                found.append((f"nas-log-errors.yml/{rule['uid']}", expr))
    return found


def test_every_severity_filter_is_case_insensitive():
    """`detected_level = "error"` silently sees only half the errors on the NAS.

    Alloy attaches the level for the Python services in this repo by parsing their
    `%(levelname)s` directly, so those arrive as ERROR. Everything else -- Grafana,
    InfluxDB, any third-party image -- is classified by Loki's own heuristic, which emits
    lowercase. Match one spelling and the other's errors are simply absent: no error, no
    empty panel, just a shorter list that looks complete.

    This is the failure mode the whole log stack exists to prevent, so it gets a test
    rather than a comment.
    """
    queries = _detected_level_queries()
    assert queries, "no severity filters found -- has the query shape changed?"
    missing = [where for where, q in queries if CASE_INSENSITIVE not in q]
    assert missing == [], f"severity filter is case-sensitive in: {missing}"


def test_the_log_error_alert_treats_no_data_as_healthy():
    """The one setting on this rule that is easy to get backwards.

    `count_over_time` returns no series when nothing matched, so "no errors in five
    minutes" and "Loki is unreachable" arrive identically. noDataState: Alerting would
    therefore fire continuously on a quiet, working NAS -- and a rule that is always red
    is a rule nobody reads. Loki being down is covered by its own container healthcheck.
    """
    alert = yaml.safe_load(NAS_ALERT.read_text("utf-8"))
    (rule,) = alert["groups"][0]["rules"]
    assert rule["noDataState"] == "OK"
    # And the debounce, which is what keeps a single transient ERROR from paging: the
    # collector logs one on a failed poll and recovers on the next.
    assert rule["for"] == "5m"


NUMERIC = re.compile(r"-?\d+(\.\d+)?$")


def _string_valued_stat_panels():
    """Stat panels whose value mappings key on strings, so the field is a string."""
    for path in DASHBOARDS:
        dash = json.loads(path.read_text(encoding="utf-8"))
        for panel in dash.get("panels", []):
            if panel.get("type") != "stat":
                continue
            keys = [
                key
                for mapping in panel["fieldConfig"]["defaults"].get("mappings", [])
                if mapping.get("type") == "value"
                for key in mapping.get("options", {})
            ]
            if any(not NUMERIC.match(key) for key in keys):
                yield path.name, panel


def test_string_valued_stat_panels_reduce_over_every_field():
    """A verdict tile must not leave `reduceOptions.fields` empty.

    Empty means "numeric fields only". These panels emit one string column, so the
    reducer finds nothing to reduce and the tile renders the null mapping -- NO DATA, or
    whatever `noValue` says -- while the query underneath is returning a perfectly good
    row. The board then reports a dead subsystem that is alive, which is the failure that
    costs the most trust: every other tile on it becomes suspect.

    Every verdict tile in this repo is built the same way (a `map()` chain ending in a
    single `_value` string) so the requirement is universal, and the symptom points at
    the query rather than the panel, which is why it gets a test instead of a comment.
    """
    wrong = [
        f"{name}: {panel['title']}"
        for name, panel in _string_valued_stat_panels()
        if panel["options"]["reduceOptions"].get("fields") != "/.*/"
    ]
    assert wrong == [], f"string-valued stat panels not reducing over all fields: {wrong}"


def test_there_are_string_valued_stat_panels_to_check():
    """The heuristic above finds nothing if the mapping shape ever changes."""
    assert len(list(_string_valued_stat_panels())) >= 8
