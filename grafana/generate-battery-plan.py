"""Emit alphaess-battery-plan.json. This script is the source; the JSON is its output.

    python grafana/generate-battery-plan.py grafana/alphaess-battery-plan.json

Alone among the dashboards here, this one is generated rather than exported from the
Grafana UI, because every Flux query in it was developed and checked against the live
database first and those queries are the substance of the dashboard.

Edit here, never in the JSON. tests/test_grafana_provisioning.py re-runs this script and
compares, so a hand-edit to the JSON fails the suite -- which is the point: 850 lines of
generated JSON is not something anyone reviews, and a wrong query buried in it renders as
a plausible chart rather than as an error.

Trying things out in the Grafana UI is still fine, but the change has to come back here to
survive: Grafana overwrites a provisioned dashboard from the file, and this file is built
from this script.
"""
import json, sys

DS = {"type": "influxdb", "uid": "${DS_ALPHAESS}"}

# Every panel that shows "the plan" has to agree on which plan that is. Picked by parsing
# plan_run to a time rather than sorting the tag as a string: the planner writes UTC now, but
# points written before 2026-07-30 carry a local offset, and a string sort mixes those two
# formats wrongly. Parsing is correct for both.
NEWEST = '''newest = (from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r._field == "soc_wh")
  |> keep(columns: ["plan_run"])
  |> group()
  |> distinct(column: "plan_run")
  |> map(fn: (r) => ({ tag: r._value, t: time(v: r._value) }))
  |> sort(columns: ["t"], desc: true)
  |> limit(n: 1)
  |> findColumn(fn: (key) => true, column: "tag"))[0]
'''

# Every charted series must leave Flux in a column named after itself, not in `_value`.
# Grafana names a field after its column, but special-cases `_value` to the literal
# "Value" - and `map(fn: (r) => ({_time: ..., _value: ...}))` drops the tag columns that
# would otherwise have distinguished the series. Four queries then arrive as four fields
# all called "Value", every `byName` override misses, and the panel silently falls back to
# its defaults: prices drawn as filled areas on the kWh axis, dashes and colours ignored.
# Seen on the NAS 2026-07-30. Renaming the column is what makes the overrides bind.
PLAN_AGE = '''import "array"

newestT = (from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r._field == "soc_wh")
  |> keep(columns: ["plan_run"])
  |> group()
  |> distinct(column: "plan_run")
  |> map(fn: (r) => ({ _value: time(v: r._value) }))
  |> max()
  |> findColumn(fn: (key) => true, column: "_value"))[0]

array.from(rows: [{
  _time: now(),
  _value: float(v: int(v: now()) - int(v: newestT)) / 1000000000.0
}])
  |> yield(name: "plan age")
'''


# The dashed lines on the price panel and the "What to set in the app" table both read the
# `app_setting` measurement, which the planner writes alongside the plan itself
# (battery-planning: app_bands.py, Marstek-planning.py appSettingLines).
#
# Two earlier versions of this lived here and both were wrong in instructive ways. First,
# textbox constants holding whatever the alphaess app was set to on 2026-08-01 - so the
# panel read as advice while actually being a stale record of an input, and the plan
# visibly contradicted it: on 2026-08-03 it charged at 8.95 ct with the line drawn at 5.73.
# Then band detection in Flux, which found the right bands but only ever computed the
# marginal price - the threshold that catches every planned trade, saying nothing about
# whether it also triggers trades the plan did not want.
#
# That second constraint is the whole difficulty, and it is arithmetic rather than query:
# a threshold has to fire on every interval the plan trades in AND stay silent on every
# interval it does not, over the whole time the setting is live. Sometimes no such number
# exists. Deciding that needs tests over seeded scenarios, and a query buried in generated
# dashboard JSON cannot have any - so it moved to Python, where 41 tests simulate the app
# against each recommendation and check the traded set equals the plan's.
#
# What is left here is a read. `exact` says whether the number does what it claims; when it
# does not, `extra_intervals` counts the trades that go against the plan.
def threshold_line(action, series):
    """A stepped dashed line: each session's threshold, drawn across that session only.

    app_setting stores one point per session, at its start. Spreading that back over a
    15-minute grid is what aggregateWindow + fill do here - createEmpty lays down the grid,
    fill carries the value forward - and joining the same treatment of `until_s` is what
    stops it carrying on past the end of the session. A flat line across the whole panel
    would have to be the loosest of all the sessions: wrong for every one but a single one,
    and an invitation for the app to trade in the hours between them.

    A session that began before the panel's left edge is not drawn, because its point lies
    outside the queried range. The table below has the same limit.
    """
    def field(name):
        return '''from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "app_setting" and r.plan_run == newest
                    and r.action == "''' + action + '''" and r._field == "''' + name + '''")
  |> group()
  |> aggregateWindow(every: 15m, fn: last, timeSrc: "_start", createEmpty: true)
  |> fill(usePrevious: true)
  |> keep(columns: ["_time", "_value"])
'''
    return '''import "join"

''' + NEWEST + '''
setTo = ''' + field("set_to_eur_kwh") + '''
liveUntil = ''' + field("until_s") + '''
join.inner(
  left: setTo,
  right: liveUntil,
  on: (l, r) => l._time == r._time,
  as: (l, r) => ({ _time: l._time, ct: l._value * 100.0,
                   untilNs: int(v: r._value) * 1000000000 }))
  |> filter(fn: (r) => int(v: r._time) < r.untilNs)
  |> map(fn: (r) => ({ _time: r._time, "''' + series + '''": r.ct }))
  |> group()
  |> yield(name: "''' + series + '''")
'''


# One row per trading session. `action` is a tag, so it survives the pivot as a column and
# no second query is needed to tell the two directions apart - which is also why this is a
# single target rather than the two-target merge the Flux version needed.
SETTINGS_TABLE = NEWEST + '''
from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "app_setting" and r.plan_run == newest)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> map(fn: (r) => ({
      from_t: r._time,
      // until_s is seconds because an InfluxDB field cannot hold a time.
      until_t: time(v: int(v: r.until_s) * 1000000000),
      action: if r.action == "sell" then "sell above" else "buy below",
      set_to: r.set_to_eur_kwh * 100.0,
      target_soc: r.target_soc_wh / float(v: ${capacity_wh}) * 100.0,
      // Spelled out rather than left as a 0/1: a column of bare zeroes beside a column of
      // prices reads as a price of zero, and the count is the part worth acting on.
      exact: if r.exact > 0.0 then "yes"
             else "no - " + string(v: int(v: r.extra_intervals)) + " interval(s) against plan"
    }))
  |> sort(columns: ["from_t"])
  |> yield(name: "app settings")
'''


def target(query, ref="A"):
    return {"datasource": DS, "query": query, "refId": ref}


def stat(id_, title, desc, query, unit, decimals, x, w, steps, color_mode="value"):
    return {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "decimals": decimals,
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": steps},
                "unit": unit,
            },
            "overrides": [],
        },
        "gridPos": {"h": 4, "w": w, "x": x, "y": 0},
        "id": id_,
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "percentChangeColorMode": "standard",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showPercentChange": False,
            "textMode": "auto",
            "wideLayout": True,
        },
        "pluginVersion": "11.6.0",
        "targets": [target(query)],
        "title": title,
        "type": "stat",
    }


def timeseries(id_, title, desc, targets, y, h, unit, overrides, fill=10, style="line"):
    return {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "axisBorderShow": False,
                    "axisCenteredZero": False,
                    "axisColorMode": "text",
                    "axisLabel": "",
                    "axisPlacement": "auto",
                    "barAlignment": 0,
                    "barWidthFactor": 0.6,
                    "drawStyle": style,
                    "fillOpacity": fill,
                    "gradientMode": "none",
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                    "insertNulls": False,
                    "lineInterpolation": "stepAfter",
                    "lineWidth": 2,
                    "pointSize": 5,
                    "scaleDistribution": {"type": "linear"},
                    "showPoints": "never",
                    "spanNulls": False,
                    "stacking": {"group": "A", "mode": "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "unit": unit,
            },
            "overrides": overrides,
        },
        "gridPos": {"h": h, "w": 24, "x": 0, "y": y},
        "id": id_,
        "options": {
            "legend": {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"},
        },
        "pluginVersion": "11.6.0",
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }


def series_override(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


panels = []

# --- Row 1: four stats ------------------------------------------------------------------
panels.append(stat(
    1, "Plan age",
    "Time since the newest plan was made. The schedule runs every 3 hours, so anything past "
    "4 hours means a run did not fire - which is otherwise silent, because a stale plan still "
    "renders perfectly well.",
    PLAN_AGE, "s", 0, 0, 6,
    [{"color": "green", "value": None},
     {"color": "orange", "value": 12600},
     {"color": "red", "value": 14400}]))

panels.append(stat(
    2, "Planned benefit over horizon",
    "Sum of the plan's per-interval cost, positive meaning earning. Matches the 'total "
    "benefit' line that advise.py prints for the same run.",
    NEWEST + '''
from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest and r._field == "cost_eur")
  |> group()
  |> sum()
  |> yield(name: "benefit")
''', "currencyEUR", 2, 6, 6,
    [{"color": "red", "value": None}, {"color": "green", "value": 0}]))

panels.append(stat(
    3, "Planned SoC at horizon end",
    "Where the plan leaves the battery. Compare with the reserve beside it: equal means the "
    "reserve constraint is binding and is the only thing stopping an end-of-window dump.",
    NEWEST + '''
from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest and r._field == "soc_wh")
  |> last()
  |> map(fn: (r) => ({ r with _value: r._value / float(v: ${capacity_wh}) * 100.0 }))
  |> yield(name: "end SoC")
''', "percent", 0, 12, 6,
    [{"color": "text", "value": None}]))

panels.append(stat(
    4, "Terminal reserve",
    "The floor the plan must not end below, recomputed every run. One number for the whole "
    "horizon, not a per-interval decision.",
    NEWEST + '''
from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest and r._field == "reserve_wh")
  |> last()
  |> map(fn: (r) => ({ r with _value: r._value / float(v: ${capacity_wh}) * 100.0 }))
  |> yield(name: "reserve")
''', "percent", 0, 18, 6,
    [{"color": "text", "value": None}]))

# --- Panel: planned vs actual SoC -------------------------------------------------------
panels.append(timeseries(
    5, "Planned SoC vs actual SoC",
    "The plan running ahead of the measured line. Nothing executes the plan - the battery "
    "follows its own self-consumption logic - so the gap between the two lines is the "
    "question this whole project exists to answer, not a fault to be corrected.",
    [target(NEWEST + '''
from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest and r._field == "soc_wh")
  |> map(fn: (r) => ({ _time: r._time, "planned": r._value / float(v: ${capacity_wh}) * 100.0 }))
  |> yield(name: "planned")
''', "A"),
     target('''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "soc_percent")
  |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ _time: r._time, "actual": r._value }))
  |> yield(name: "actual")
''', "B"),
     target(NEWEST + '''
from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest and r._field == "reserve_wh")
  |> map(fn: (r) => ({ _time: r._time, "reserve": r._value / float(v: ${capacity_wh}) * 100.0 }))
  |> yield(name: "reserve")
''', "C")],
    4, 9, "percent",
    [series_override("planned", [{"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}}]),
     series_override("actual", [{"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                                {"id": "custom.fillOpacity", "value": 0}]),
     series_override("reserve", [{"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                                 {"id": "custom.fillOpacity", "value": 0},
                                 {"id": "custom.lineStyle",
                                  "value": {"dash": [10, 10], "fill": "dash"}}])],
    fill=8))

# --- Panel: actions against price -------------------------------------------------------
panels.append(timeseries(
    6, "Planned charge / discharge, against price",
    "Discharge is drawn negative so the two directions separate. The price line is the raw "
    "market price (no tax, sourcing markup, or energy tax) - the same signal alphaess's own "
    "scheduling reacts to, so this is what to eyeball when tuning it: buying should happen in "
    "the troughs, selling on the peaks. If an action does not line up with the price, that is "
    "the interesting case. The dashed lines are derived from the plan, not from the app: "
    "each step is what the app's High/Low band must be set to during that band for the app "
    "to make the trade the plan wants. They step because one global pair cannot serve every "
    "band - see the table below for when to change them.",
    [target(NEWEST + '''
from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest)
  |> filter(fn: (r) => r._field == "charge_wh" or r._field == "discharge_wh")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({
      _time: r._time,
      "charge": r.charge_wh / 1000.0,
      "discharge": -r.discharge_wh / 1000.0 }))
  |> yield(name: "actions")
''', "A"),
     target('''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "market_price" and r._field == "market_price")
  |> map(fn: (r) => ({ _time: r._time, "market price": r._value * 100.0 }))
  |> yield(name: "market_price")
''', "B"),
     target(threshold_line("sell", "sell above"), "C"),
     target(threshold_line("buy", "buy below"), "D")],
    13, 9, "kwatth",
    [series_override("charge", [{"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}},
                                {"id": "custom.drawStyle", "value": "bars"}]),
     series_override("discharge", [{"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}},
                                   {"id": "custom.drawStyle", "value": "bars"}]),
     series_override("market price", [{"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                                      {"id": "unit", "value": "none"},
                                      {"id": "custom.axisPlacement", "value": "right"},
                                      {"id": "custom.axisLabel", "value": "ct/kWh"},
                                      {"id": "custom.fillOpacity", "value": 0}]),
     # The three right-hand series must agree on unit *and* axisLabel. Grafana builds a
     # series' y-scale key from both, so an identical unit with a different label lands on
     # a second, independently auto-ranged right axis - and two lines that cannot be
     # compared is precisely what this panel is for. Seen on the NAS 2026-08-02: the price
     # drew on 0..25 while the thresholds drew on 4..18, putting the dashed sell line about
     # 23 percentage points of panel height above where the price scale would have placed
     # it. Same class of failure as the `_value`/"Value" trap above - it renders as a
     # perfectly plausible chart. test_grafana_provisioning.py pins this.
     # Purple, not the dark-red it used to be. Sharing one axis with the price is the whole
     # point of the fix above, and its side effect is that this line now sits in the middle
     # of the plot, crossing a red price curve - two reds a shade apart, exactly where they
     # have to be told apart. It read fine only while the broken axis parked it near the top.
     # Purple also survives red/green colour blindness beside the green `buy below`, which
     # dark-red did not.
     series_override("sell above", [{"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
                                    {"id": "unit", "value": "none"},
                                    {"id": "custom.axisPlacement", "value": "right"},
                                    {"id": "custom.axisLabel", "value": "ct/kWh"},
                                    {"id": "custom.fillOpacity", "value": 0},
                                    {"id": "custom.lineStyle",
                                     "value": {"dash": [10, 10], "fill": "dash"}},
                                    {"id": "custom.lineWidth", "value": 1}]),
     series_override("buy below", [{"id": "color", "value": {"fixedColor": "dark-green", "mode": "fixed"}},
                                   {"id": "unit", "value": "none"},
                                   {"id": "custom.axisPlacement", "value": "right"},
                                   {"id": "custom.axisLabel", "value": "ct/kWh"},
                                   {"id": "custom.fillOpacity", "value": 0},
                                   {"id": "custom.lineStyle",
                                    "value": {"dash": [10, 10], "fill": "dash"}},
                                   {"id": "custom.lineWidth", "value": 1}])],
    fill=60))

# --- Panel: app settings table ----------------------------------------------------------
# Two targets rather than one union, merged by transformation: the sell and buy queries
# differ in four places and reading them side by side is how the asymmetry stays visible.
panels.append({
    "datasource": DS,
    "description": "What to type into the alphaess app, and when. The app takes one "
                   "High/Low pair at a time, so each row is a setting to enter at 'From' "
                   "and replace when the next row of the same direction comes due. "
                   "'to ct/kWh' is chosen so the app trades in exactly the intervals the "
                   "plan trades in - not merely the ones it must catch, but also none of "
                   "the ones it must leave alone, for the whole time the setting is live. "
                   "Raw market price, matching the graph above. 'Exact' is no when no "
                   "single threshold can do both, and counts the intervals that then go "
                   "against the plan; treat those rows as the best available compromise "
                   "rather than an instruction. 'Target SoC' is what the battery should "
                   "read by 'Until'. Nothing here is read by the optimiser - it is the "
                   "reverse, these numbers are worked backwards out of the plan so the app "
                   "can be made to follow it.",
    "fieldConfig": {
        "defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [
            {"matcher": {"id": "byName", "options": "from_t"},
             "properties": [{"id": "displayName", "value": "From"}]},
            {"matcher": {"id": "byName", "options": "until_t"},
             "properties": [{"id": "displayName", "value": "Until"}]},
            {"matcher": {"id": "byName", "options": "action"},
             "properties": [{"id": "displayName", "value": "Set"}]},
            {"matcher": {"id": "byName", "options": "set_to"},
             "properties": [{"id": "decimals", "value": 2},
                            {"id": "displayName", "value": "to ct/kWh"}]},
            {"matcher": {"id": "byName", "options": "target_soc"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "Target SoC"}]},
            {"matcher": {"id": "byName", "options": "exact"},
             "properties": [{"id": "displayName", "value": "Exact"}]},
        ],
    },
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 22},
    "id": 8,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": False, "displayName": "From"}],
    },
    "pluginVersion": "11.6.0",
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {},
            "includeByName": {},
            "renameByName": {},
            "indexByName": {"from_t": 0, "until_t": 1, "action": 2, "set_to": 3,
                            "target_soc": 4, "exact": 5},
        }},
    ],
    "targets": [target(SETTINGS_TABLE)],
    "title": "What to set in the app",
    "type": "table",
})

# --- Panel: action table ----------------------------------------------------------------
panels.append({
    "datasource": DS,
    "description": "Every interval the plan does something in. Not the merged blocks that "
                   "advise.py prints - those are built in Python from the same numbers and "
                   "are not stored, so reproducing them here would mean writing the "
                   "merge twice and letting the two drift. ct/kWh is the raw market price, "
                   "the same number the graph above plots - not the all-in price the plan "
                   "optimises against, which includes tax, sourcing markup and energy tax. "
                   "Blank where the day-ahead has not been published yet. "
                   "Where the list stops is usually the terminal reserve binding, not the "
                   "price becoming unattractive: check whether the last row's SoC equals "
                   "the Terminal reserve above. The optimiser follows no price threshold at "
                   "all - it solves the whole horizon. The sell/buy lines on the graph are "
                   "read back out of these actions, not fed into them.",
    "fieldConfig": {
        "defaults": {
            "custom": {
                "align": "auto",
                "cellOptions": {"type": "auto"},
                "inspect": False,
            },
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [
            {"matcher": {"id": "byName", "options": "kWh"},
             "properties": [{"id": "decimals", "value": 2}]},
            {"matcher": {"id": "byName", "options": "soc_pct"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "SoC after"}]},
            {"matcher": {"id": "byName", "options": "ct_kWh"},
             "properties": [{"id": "decimals", "value": 1},
                            {"id": "displayName", "value": "ct/kWh"}]},
        ],
    },
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": 30},
    "id": 7,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": False, "displayName": "Time"}],
    },
    "pluginVersion": "11.6.0",
    # Column order is set here rather than by the order of fields in the Flux `map`: Flux
    # does not promise an output column order, so a map that happens to come out right
    # today would reorder itself on an unrelated change and nobody would notice.
    "transformations": [{
        "id": "organize",
        "options": {
            "excludeByName": {},
            "includeByName": {},
            "renameByName": {},
            "indexByName": {"_time": 0, "action": 1, "ct_kWh": 2, "soc_pct": 3, "kWh": 4},
        },
    }],
    "targets": [target('''import "join"

''' + NEWEST + '''
plan = from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest)
  |> filter(fn: (r) => r._field == "charge_wh" or r._field == "discharge_wh"
                    or r._field == "soc_wh")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => r.charge_wh > 0.0 or r.discharge_wh > 0.0)
  |> group()

// timeSrc: "_start" because aggregateWindow otherwise labels each window by its stop,
// which would slide every price 15 minutes past the interval it belongs to - a table that
// still looks entirely reasonable. createEmpty + fill carry an hourly price across the
// quarters it covers, for days before the 2026-08-01 15-min cutover.
mkt = from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "market_price" and r._field == "market_price")
  |> aggregateWindow(every: 15m, fn: last, timeSrc: "_start", createEmpty: true)
  |> fill(usePrevious: true)
  |> group()
  |> keep(columns: ["_time", "_value"])

// Left, not inner: the plan runs past the published day-ahead, and an inner join would
// drop those intervals rather than show them with no price. A table that silently ends
// early is worse than one with blank cells.
join.left(
  left: plan,
  right: mkt,
  on: (l, r) => l._time == r._time,
  as: (l, r) => ({
      _time: l._time,
      action: if l.charge_wh > 0.0 then "charge" else "discharge",
      kWh: (if l.charge_wh > 0.0 then l.charge_wh else l.discharge_wh) / 1000.0,
      soc_pct: l.soc_wh / float(v: ${capacity_wh}) * 100.0,
      ct_kWh: r._value * 100.0
    }))
  |> yield(name: "actions")
''')],
    "title": "Planned actions",
    "type": "table",
})

dashboard = {
    "__inputs": [{"name": "DS_ALPHAESS", "label": "alphaess", "description": "",
                  "type": "datasource", "pluginId": "influxdb", "pluginName": "InfluxDB"}],
    "__elements": {},
    "__requires": [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "11.6.0"},
        {"type": "datasource", "id": "influxdb", "name": "InfluxDB", "version": "1.0.0"},
        {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
        {"type": "panel", "id": "table", "name": "Table", "version": ""},
        {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
    ],
    "annotations": {"list": [{
        "builtIn": 1,
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard",
    }]},
    "description": "What the planner intended, from the planning bucket, against what the "
                   "battery actually did. Advisory only - nothing here is executed.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "links": [],
    "panels": panels,
    "preload": False,
    "refresh": "5m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "plan"],
    "templating": {"list": [{
        "current": {"text": "27900", "value": "27900"},
        "description": "Usable battery capacity in Wh. The plan stores SoC in Wh; every "
                       "percentage on this dashboard divides by this. It is not in InfluxDB, "
                       "so it lives here where it can be seen rather than buried in six queries.",
        # textbox, not constant: Grafana hides constant variables entirely, which is the
        # opposite of the point - this number has to be visible and changeable if the
        # battery ever does.
        "hide": 0,
        "label": "Capacity (Wh)",
        "name": "capacity_wh",
        "options": [{"selected": True, "text": "27900", "value": "27900"}],
        "query": "27900",
        "skipUrlSync": False,
        "type": "textbox",
    }]},
    # price_high_eur / price_low_eur used to live here, holding 0.16472 / 0.05733 - a
    # snapshot of the alphaess app taken on 2026-08-01 and never updated again. Nothing kept
    # them in step with the app, and the panel's own purpose was retuning the app, so using
    # the dashboard as intended was what made the numbers wrong. They are now computed from
    # the plan instead; see threshold_line and SETTINGS_TABLE.
    # Six hours back, thirty-six forward: enough past to see the actual line diverge, enough
    # future to cover a horizon built after the ~13:00 price release.
    "time": {"from": "now-6h", "to": "now+36h"},
    "timepicker": {},
    "timezone": "browser",
    "title": "AlphaESS Battery Plan",
    "uid": "alphaess-battery-plan",
    # BUMP THIS on every change below. Grafana's file provisioner keeps the dashboard it
    # already stored unless the incoming version is higher - it reads the new file, compares,
    # and does nothing, with no error and no log line. Restarting or recreating the container
    # does not help. The symptom is a fix that appears not to have worked, which sends you
    # back to re-debug a query that was already correct.
    # 2: series renamed out of _value so the byName overrides bind.
    # 3: price line switched to raw market price; sell/buy threshold lines added.
    "version": 7,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
