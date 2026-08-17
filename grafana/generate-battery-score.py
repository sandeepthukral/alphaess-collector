"""Emit alphaess-battery-score.json. This script is the source; the JSON is its output.

    python grafana/generate-battery-score.py grafana/alphaess-battery-score.json

The retrospective half of the pair. `alphaess-battery-plan.json` shows what the planner
intends over the next day and a half; this one shows how yesterday and the days before it
actually turned out. They are separate dashboards rather than two halves of one because
their time ranges point in opposite directions -- the plan dashboard opens on
now-6h .. now+36h, and a retrospective panel on that range is a sliver of an evening.

The numbers come from the `plan_score` measurement, which `report_day.py` in the
battery-planning repo writes once a day at 06:10. That is deliberate: the report computes
"which plan was in force for this interval" and prices every interval at the price stored
with the plan, and neither is expressible in Flux. Recomputing here would produce a second
answer that quietly disagrees with the text report. So every panel below reads stored
numbers and does no arithmetic beyond summing them.

`plan_score` carries no tags. Unlike `plan`, it is one series, so nothing here has to pick
"the newest run" the way the plan dashboard does.

Like its sibling this is generated, not exported from the Grafana UI, and
tests/test_grafana_provisioning.py re-runs the script and compares. Edit here, never in
the JSON.
"""
import json, sys

DS = {"type": "influxdb", "uid": "${DS_ALPHAESS}"}

# Capacity, read from the plan rather than typed in here. See generate-battery-plan.py for
# why the query is shaped the way it is; the string is duplicated because these two scripts
# share no module, and tests/test_grafana_provisioning.py pins every dashboard to the same
# query for exactly that reason.
#
# `plan` is the only thing this dashboard reads outside `plan_score`, which carries no
# capacity of its own. Taking the newest run means a capacity change re-renders older days at
# the new number -- the same thing the literal did, only visibly and all at once instead of
# whenever someone remembered to edit three files.
CAPACITY_QUERY = '''from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r._field == "capacity_wh")
  |> group()
  |> map(fn: (r) => ({_value: r._value, _run: time(v: r.plan_run)}))
  |> sort(columns: ["_run"], desc: true)
  |> limit(n: 1)
  |> map(fn: (r) => ({_value: string(v: int(v: r._value))}))'''

CAPACITY_VAR = {
    "current": {},
    "datasource": DS,
    "definition": "capacity_wh from the newest plan point",
    "description": "Usable battery capacity in Wh, read from the newest `plan` point the "
                   "planner wrote. plan_score stores SoC in Wh on both sides; this is what "
                   "the SoC panel divides by. It no longer has to be kept equal to the plan "
                   "dashboard and to BT_CAP by hand -- all three now read the planner.",
    "hide": 0,
    "label": "Capacity (Wh)",
    "name": "capacity_wh",
    "options": [],
    "query": CAPACITY_QUERY,
    "refresh": 1,
    "regex": "",
    "skipUrlSync": False,
    "sort": 0,
    "type": "query",
}

# Every panel starts from the same three lines. Kept as one string so a change to the
# bucket or measurement cannot be applied to eight queries and missed on the ninth.
SCORE = '''from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan_score")'''

# aggregateWindow aligns its windows to the Unix epoch, which is UTC midnight -- so a "1d"
# bucket without this covers 02:00 to 02:00 local in summer and 01:00 to 01:00 in winter,
# and the money attributed to a day would include two hours of the next one. Only the
# panels that window by day need it; v.windowPeriod panels are not claiming to be days.
LOCATION = '''import "timezone"
option location = timezone.location(name: "Europe/Amsterdam")

'''

# Time since the last scored interval. Deliberately not on v.timeRangeStart: "is the report
# still running" is a fact about now, and reading it off the dashboard's picker would make
# it go red merely because someone looked at last week.
#
# The scored day ends at midnight and report.sh writes it at 06:10, so this reads ~6h right
# after a run and ~30h just before the next one. Past 32h a run did not fire -- which is
# otherwise completely silent, because yesterday's score still renders perfectly well.
SCORE_AGE = '''import "array"

lastT = (from(bucket: "planning")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "plan_score" and r._field == "cost_eur_actual")
  |> last()
  |> findColumn(fn: (key) => true, column: "_time"))[0]

array.from(rows: [{
  _time: now(),
  _value: float(v: int(v: now()) - int(v: lastT)) / 1000000000.0
}])
  |> yield(name: "score age")
'''


def target(query, ref="A"):
    return {"datasource": DS, "query": query, "refId": ref}


def sumOf(field, name):
    """Total of one field over the picked range. One field per query rather than a pivot:
    cost_eur_nobattery is only written for intervals where measured PV and load both exist,
    and a pivot referencing a column that some rows lack fails the whole panel."""
    return SCORE + '''
  |> filter(fn: (r) => r._field == "%s")
  |> group()
  |> sum()
  |> yield(name: "%s")
''' % (field, name)


def diffOf(plus, minus, name):
    """Sum of one field minus the sum of another, without pivoting.

    Flipping the sign of one field and summing everything gives the same answer as summing
    twice and subtracting, and it survives a field being absent on some intervals -- which
    the equivalent pivot does not. It does assume the two fields are written together; they
    are, both being computed from the same row in report_day.py.
    """
    return SCORE + '''
  |> filter(fn: (r) => r._field == "%s" or r._field == "%s")
  |> map(fn: (r) => ({ r with _value: if r._field == "%s" then -r._value else r._value }))
  |> group()
  |> sum()
  |> yield(name: "%s")
''' % (plus, minus, minus, name)


def errorOf(forecast, actual, name):
    """Forecast error as a percentage of what was measured, over the whole picked range.

    A ratio of two sums, not the mean of per-interval ratios: an interval where the panels
    made 3 Wh and the forecast said 30 is a 900% error that means nothing, and averaging
    those swamps the answer. Summing first weights every interval by the energy in it.
    """
    # `import` has to be the first statement in the file, not next to the call that needs
    # it: Flux rejects a mid-script import with "invalid statement: import".
    return '''import "array"

sums = ''' + SCORE + '''
  |> filter(fn: (r) => r._field == "%s" or r._field == "%s")
  |> group(columns: ["_field"])
  |> sum()

f = (sums |> filter(fn: (r) => r._field == "%s")
          |> findColumn(fn: (key) => true, column: "_value"))[0]
a = (sums |> filter(fn: (r) => r._field == "%s")
          |> findColumn(fn: (key) => true, column: "_value"))[0]

array.from(rows: [{
  _time: now(),
  _value: if a == 0.0 then 0.0 else (f - a) / a * 100.0
}])
  |> yield(name: "%s")
''' % (forecast, actual, forecast, actual, name)


def daily(field, name, scale=1.0):
    """One bar per local day. timeSrc: "_start" because aggregateWindow otherwise stamps a
    window with its END, which would draw Thursday's money at Friday 00:00."""
    return LOCATION + SCORE + '''
  |> filter(fn: (r) => r._field == "%s")
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false, timeSrc: "_start")
  |> map(fn: (r) => ({ _time: r._time, "%s": r._value%s }))
  |> yield(name: "%s")
''' % (field, name, "" if scale == 1.0 else " / (%s)" % scale, name)


def windowed(field, name, fn="sum", scale=1.0):
    """v.windowPeriod, so the same panel is readable whether the picker holds one day or a
    month -- Grafana sizes the window to the panel's pixel width. Energy sums stay correct
    at any window; only the bar count changes."""
    return SCORE + '''
  |> filter(fn: (r) => r._field == "%s")
  |> aggregateWindow(every: v.windowPeriod, fn: %s, createEmpty: false)
  |> map(fn: (r) => ({ _time: r._time, "%s": r._value%s }))
  |> yield(name: "%s")
''' % (field, fn, name, "" if scale == 1.0 else " / (%s)" % scale, name)


def stat(id_, title, desc, query, unit, decimals, x, y, steps, w=6, color_mode="value"):
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
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
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


def timeseries(id_, title, desc, targets, y, h, unit, overrides,
               fill=10, style="line", w=24, x=0):
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
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
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


def fixed(color):
    return {"id": "color", "value": {"fixedColor": color, "mode": "fixed"}}


DASHED = {"id": "custom.lineStyle", "value": {"dash": [10, 10], "fill": "dash"}}
NOFILL = {"id": "custom.fillOpacity", "value": 0}
BARS = {"id": "custom.drawStyle", "value": "bars"}

# Cost is signed the way a meter is: positive means money left the house. Every euro stat
# below therefore reads green when it is negative, which takes a second to get used to and
# is worth it -- it means the three cost numbers and the report's own totals are the same
# number, rather than one of them being negated somewhere for display.
MONEY_STEPS = [{"color": "green", "value": None}, {"color": "red", "value": 0}]
GAIN_STEPS = [{"color": "red", "value": None}, {"color": "green", "value": 0}]
NEUTRAL = [{"color": "text", "value": None}]

panels = []

# --- Row 1: is the report running, and what did the meter do ---------------------------
panels.append(stat(
    1, "Score age",
    "Time since the last scored interval. report.sh runs at 06:10 for the day that just "
    "ended, so this sits between 6 and 30 hours in normal operation. Above 32 hours a run "
    "did not fire, which nothing else on this dashboard would show: yesterday's score "
    "still renders perfectly well.",
    SCORE_AGE, "s", 0, 0, 0,
    [{"color": "green", "value": None},
     {"color": "orange", "value": 115200},
     {"color": "red", "value": 144000}]))

panels.append(stat(
    2, "At the meter, actual",
    "What the grid connection actually cost over the picked range, at the prices stored "
    "with the plan. Negative means the house earned money. This is the real number; the "
    "two beside it are what-ifs.",
    sumOf("cost_eur_actual", "actual"), "currencyEUR", 2, 6, 0, MONEY_STEPS))

panels.append(stat(
    3, "At the meter, as planned",
    "The same range priced as if the battery had followed the plan in force for each "
    "interval. Nothing executes these plans - the AlphaESS runs its own self-consumption "
    "logic - so the gap between this and the actual is the value of the advice, not a "
    "measure of how well it was followed.",
    sumOf("cost_eur_plan", "planned"), "currencyEUR", 2, 12, 0, MONEY_STEPS))

panels.append(stat(
    4, "At the meter, without a battery",
    "Measured load and measured solar, every shortfall bought and every surplus sold at "
    "the same prices. The baseline the whole system is judged against.",
    sumOf("cost_eur_nobattery", "no battery"), "currencyEUR", 2, 18, 0, MONEY_STEPS))

# --- Row 2: what the battery and the forecasts were worth -------------------------------
panels.append(stat(
    5, "Battery benefit",
    "No-battery baseline minus what actually happened. Positive means the battery saved "
    "money over the picked range.\n\nRead it together with the SoC panel below: a range "
    "that opens with a full battery and closes with an empty one books earnings for energy "
    "bought before the range began. Only a whole number of days is honest.",
    diffOf("cost_eur_nobattery", "cost_eur_actual", "benefit"),
    "currencyEUR", 2, 0, 4, GAIN_STEPS))

panels.append(stat(
    6, "Cost of not following the plan",
    "Actual minus planned. Positive means the day cost more than the plan said it would, "
    "which is the case for taking the advice. Negative means the battery's own logic beat "
    "the optimiser over this range - worth understanding rather than dismissing.",
    diffOf("cost_eur_actual", "cost_eur_plan", "gap"),
    "currencyEUR", 2, 6, 4, NEUTRAL))

panels.append(stat(
    7, "PV forecast error",
    "Forecast solar against measured, as a percentage of measured. Negative means "
    "forecast.solar predicted less than the panels made.\n\nThis is the calibrated figure "
    "the planner actually used, not the raw forecast: it has already been through the "
    "elevation curve and pvPlanningFactor. Do not fit pvOverallCalibration against this "
    "number - it would double-count corrections that are already in it.",
    errorOf("pv_wh_forecast", "pv_wh_actual", "pv error"), "percent", 0, 12, 4, NEUTRAL))

panels.append(stat(
    8, "Load forecast error",
    "Forecast household load against measured. The forecast is the recent seven days from "
    "InfluxDB with no weekday/weekend split, so a bank holiday or a week away shows up "
    "here first.",
    errorOf("load_wh_forecast", "load_wh_actual", "load error"), "percent", 0, 18, 4, NEUTRAL))

# --- Panel: money per day ---------------------------------------------------------------
panels.append(timeseries(
    9, "Money at the meter, per day",
    "Three bars a day: what happened, what the plan said would happen, and what a house "
    "with no battery would have paid. Bars below zero are days the connection earned "
    "money.\n\nDays, not hours: the price of an interval is only meaningful once the "
    "battery has had a chance to move energy out of it.",
    [target(daily("cost_eur_actual", "actual"), "A"),
     target(daily("cost_eur_plan", "planned"), "B"),
     target(daily("cost_eur_nobattery", "no battery"), "C")],
    8, 8, "currencyEUR",
    [series_override("actual", [fixed("green"), BARS]),
     series_override("planned", [fixed("blue"), BARS]),
     series_override("no battery", [fixed("red"), BARS])],
    fill=70))

# --- Panel: SoC ------------------------------------------------------------------------
panels.append(timeseries(
    10, "State of charge, planned vs actual",
    "The same comparison the plan dashboard makes live, kept after the fact. Where the "
    "two lines start and end matters as much as the distance between them - a range that "
    "ends lower than it started spent stored energy that the money panels count as "
    "earnings.",
    [target(windowed("soc_wh_plan", "planned", fn="mean", scale="float(v: ${capacity_wh}) / 100.0"), "A"),
     target(windowed("soc_wh_actual", "actual", fn="mean", scale="float(v: ${capacity_wh}) / 100.0"), "B")],
    16, 8, "percent",
    [series_override("planned", [fixed("blue"), NOFILL, DASHED]),
     series_override("actual", [fixed("green")])],
    fill=8))

# --- Panel: grid ------------------------------------------------------------------------
panels.append(timeseries(
    11, "Grid import and export, planned vs actual",
    "Export is drawn negative so the two directions separate. Import above the plan with "
    "export also above it means the day simply had more energy moving through it than "
    "forecast; import above with export below means the battery was in the wrong state "
    "when it mattered.",
    [target(windowed("import_wh_actual", "import actual", scale=1000.0), "A"),
     target(windowed("import_wh_plan", "import planned", scale=1000.0), "B"),
     target(windowed("export_wh_actual", "export actual", scale=-1000.0), "C"),
     target(windowed("export_wh_plan", "export planned", scale=-1000.0), "D")],
    24, 8, "kwatth",
    [series_override("import actual", [fixed("red"), BARS]),
     series_override("import planned", [fixed("red"), NOFILL, DASHED]),
     series_override("export actual", [fixed("green"), BARS]),
     series_override("export planned", [fixed("green"), NOFILL, DASHED])],
    fill=60))

# --- Panels: the two forecasts ----------------------------------------------------------
panels.append(timeseries(
    12, "Solar: forecast vs measured",
    "forecast.solar after calibration, against what the panels made. A forecast that is "
    "wrong in shape - right total, wrong hours - costs as much as one that is wrong in "
    "total, because the plan buys and sells on the shape.",
    [target(windowed("pv_wh_actual", "measured", scale=1000.0), "A"),
     target(windowed("pv_wh_forecast", "forecast", scale=1000.0), "B")],
    32, 8, "kwatth",
    [series_override("measured", [fixed("yellow")]),
     series_override("forecast", [fixed("orange"), NOFILL, DASHED])],
    fill=25, w=12, x=0))

panels.append(timeseries(
    13, "Load: forecast vs measured",
    "The recent-seven-days profile against the day that happened. There is no longer any "
    "historical load data to widen this with, so the immediate past is the only forecast "
    "available and this panel is the only check on it.",
    [target(windowed("load_wh_actual", "measured", scale=1000.0), "A"),
     target(windowed("load_wh_forecast", "forecast", scale=1000.0), "B")],
    32, 8, "kwatth",
    [series_override("measured", [fixed("purple")]),
     series_override("forecast", [fixed("blue"), NOFILL, DASHED])],
    fill=25, w=12, x=12))

# --- Panel: the daily table -------------------------------------------------------------
# The one place that does pivot. Over a whole day every field is present -- they are all
# written from the same row of report_day.py, and a day with grid readings but no PV or
# load readings would mean the collector returned half an API response for 24 hours.
panels.append({
    "datasource": DS,
    "description": "One row per day, newest first, matching the totals in the text report "
                   "that data/reports/report_YYYYMMDD.txt holds on the NAS. The table is "
                   "here because bars answer \"which day was better\" and a number answers "
                   "\"by how much\".",
    "fieldConfig": {
        "defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [
            {"matcher": {"id": "byName", "options": "actual"},
             "properties": [{"id": "decimals", "value": 2}, {"id": "unit", "value": "currencyEUR"},
                            {"id": "displayName", "value": "Actual"}]},
            {"matcher": {"id": "byName", "options": "planned"},
             "properties": [{"id": "decimals", "value": 2}, {"id": "unit", "value": "currencyEUR"},
                            {"id": "displayName", "value": "As planned"}]},
            {"matcher": {"id": "byName", "options": "no battery"},
             "properties": [{"id": "decimals", "value": 2}, {"id": "unit", "value": "currencyEUR"},
                            {"id": "displayName", "value": "No battery"}]},
            {"matcher": {"id": "byName", "options": "benefit"},
             "properties": [{"id": "decimals", "value": 2}, {"id": "unit", "value": "currencyEUR"},
                            {"id": "displayName", "value": "Battery benefit"}]},
            {"matcher": {"id": "byName", "options": "pv error"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "PV forecast error"}]},
            {"matcher": {"id": "byName", "options": "load error"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "Load forecast error"}]},
        ],
    },
    "gridPos": {"h": 9, "w": 24, "x": 0, "y": 40},
    "id": 14,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": True, "displayName": "Time"}],
    },
    "pluginVersion": "11.6.0",
    "targets": [target(LOCATION + SCORE + '''
  |> filter(fn: (r) => r._field == "cost_eur_actual" or r._field == "cost_eur_plan"
                    or r._field == "cost_eur_nobattery"
                    or r._field == "pv_wh_forecast" or r._field == "pv_wh_actual"
                    or r._field == "load_wh_forecast" or r._field == "load_wh_actual")
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false, timeSrc: "_start")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({
      _time: r._time,
      "actual": r.cost_eur_actual,
      "planned": r.cost_eur_plan,
      "no battery": r.cost_eur_nobattery,
      "benefit": r.cost_eur_nobattery - r.cost_eur_actual,
      "pv error": if r.pv_wh_actual == 0.0 then 0.0
                  else (r.pv_wh_forecast - r.pv_wh_actual) / r.pv_wh_actual * 100.0,
      "load error": if r.load_wh_actual == 0.0 then 0.0
                    else (r.load_wh_forecast - r.load_wh_actual) / r.load_wh_actual * 100.0
    }))
  |> yield(name: "daily")
''')],
    "title": "Day by day",
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
    "description": "Yesterday's plan against yesterday's meter, from the plan_score "
                   "measurement that report_day.py writes each morning. Advisory only - "
                   "nothing here was executed.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    # By tag rather than by uid: the forward-looking dashboard is the one thing a person
    # looking at this will want next, and a tag link keeps working if it is ever renamed.
    "links": [{
        "asDropdown": False,
        "icon": "external link",
        "includeVars": False,
        "keepTime": False,
        "tags": ["plan"],
        "targetBlank": False,
        "title": "Battery plan",
        "tooltip": "What the planner intends next",
        "type": "dashboards",
        "url": "",
    }, {
        # The battery-planning repo's report-viewer nginx service, serving report_day.py's
        # full narrative (SoC-carry notes, no-battery baseline framing, hour-by-hour
        # forecast commentary) that isn't worth re-deriving as Flux queries. Links to the
        # directory index, not a single ${__from:date:...} file - the dashboard's time-range
        # "from" is not "today", so a date-templated link opened on any past day.
        "asDropdown": False,
        "icon": "doc",
        "includeVars": False,
        "keepTime": False,
        "targetBlank": True,
        "title": "Full report (text)",
        "tooltip": "Browse report_day.py's narrative for any day",
        "type": "link",
        "url": "http://192.168.68.105:8091/",
    }],
    "panels": panels,
    "preload": False,
    # The score is written once a day. A 5-minute refresh would re-run fourteen queries
    # against a number that cannot have changed.
    "refresh": "30m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "score"],
    "templating": {"list": [CAPACITY_VAR]},
    # Whole days back and nothing forward: there is no score for a day that has not ended.
    "time": {"from": "now-7d/d", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "AlphaESS Plan vs Actual",
    "uid": "alphaess-battery-score",
    # BUMP THIS on every change below. Grafana's file provisioner keeps the dashboard it
    # already stored unless the incoming version is higher - it reads the new file, compares,
    # and does nothing, with no error and no log line. The symptom is a fix that appears not
    # to have worked, which sends you back to re-debug a query that was already correct.
    "version": 5,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
