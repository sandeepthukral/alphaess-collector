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

# The capacity every SoC percentage on this dashboard divides by, read from the plan that the
# percentage describes rather than typed in here. Until 2026-08-17 this was a textbox holding
# a literal 27900, one of nine hand-written copies across two repos and two units; the
# planner now publishes capacity_wh on every `plan` point (battery-planning PR #25), so the
# number travels with the data it explains. See PLAN-repo-seams.md Part 2a.
#
# int() is not cosmetic. The field is a float, every consumer interpolates it as
# `float(v: ${capacity_wh})`, and a value Grafana chooses to render as 2.79e+04 is not
# parseable there. Pinning it to an integer string removes the question.
#
# The shape below is NEWEST's, and for NEWEST's reason: "the newest plan" means the newest
# `plan_run`, parsed, and nothing else works. Three separate traps, all confirmed against the
# live bucket on 2026-08-17:
#
#   group() - `plan` is tagged with plan_run, so the filter yields one table per run and a
#   bare last() returns the last row of EACH. keep() drops the column but does not merge the
#   tables; only group() does. The query as first written returned 2 rows.
#
#   sort by plan_run, not by _time - runs share a horizon end. The 09:01:20Z and 09:05:03Z
#   runs both stop at 2026-08-17T21:45:00Z, because the horizon is cut at the end of the
#   priced window rather than a fixed span from the run. Picking the row with the greatest
#   _time therefore picks arbitrarily between every run still in flight, which is fine while
#   they all say 27900 and wrong on precisely the day this variable exists for.
#
#   time(v:) rather than a string sort - plan_run is UTC now, but points written before
#   2026-07-30 carry a local offset, and a string sort mixes the two formats wrongly.
#
# stop: 72h because plan points are timestamped into the future; the default stop of now()
# hides the part of a run that has not elapsed yet, and a run written minutes ago may be
# entirely in the future. -14d to match NEWEST below, deliberately: the two have to fail at the
# same moment. If this window were shorter, an outage between the two lengths would leave NEWEST
# still finding a plan to draw while the capacity variable resolved to nothing -- every panel
# rendering, and all twelve `float(v: ${capacity_wh})` sites erroring at once.
CAPACITY_QUERY = '''from(bucket: "planning")
  |> range(start: -14d, stop: 72h)
  |> filter(fn: (r) => r._measurement == "plan" and r._field == "capacity_wh")
  |> group()
  |> map(fn: (r) => ({_value: r._value, _run: time(v: r.plan_run)}))
  |> sort(columns: ["_run"], desc: true)
  |> limit(n: 1)
  |> map(fn: (r) => ({_value: string(v: int(v: r._value))}))'''

CAPACITY_VAR = {
    # No hardcoded `current`/`options`: a stale entry here is how a query variable keeps
    # serving the old constant after the query starts returning something else.
    "current": {},
    "datasource": DS,
    "definition": "capacity_wh from the newest plan point",
    "description": "Usable battery capacity in Wh, read from the newest `plan` point the "
                   "planner wrote. The plan stores SoC in Wh and every percentage here "
                   "divides by this, so it is sourced from the same run rather than kept in "
                   "step by hand.",
    # hide: 0 - still visible, and still the honest place to look when a percentage seems
    # wrong. It is no longer editable, which is the point: editing it never changed the
    # battery, only the arithmetic.
    "hide": 0,
    "label": "Capacity (Wh)",
    "name": "capacity_wh",
    "options": [],
    "query": CAPACITY_QUERY,
    # On dashboard load. The alternative, on time-range change, re-runs it for a value that
    # cannot depend on the time range.
    "refresh": 1,
    "regex": "",
    "skipUrlSync": False,
    "sort": 0,
    "type": "query",
}

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


# The price line, from two sources joined at now().
#
# It used to be the collector's own `market_price` alone: a Frank Energie feed refreshed by
# scripts/refresh-prices.sh every three hours, which publishes tomorrow later than the
# day-ahead auction the planner reads. So every afternoon the panel drew bars, SoC and
# threshold lines across a tomorrow with no price under them - which looks exactly like a
# quiet market rather than a missing series. Worse, even once filled it was a second feed:
# the line under the plan's bars need not have been the prices the plan optimised against.
#
# Ahead of now() the price is therefore the plan's own `price_market`, stored by the planner
# on the same point as the schedule. Behind now() it stays the collector's stored price,
# which is the record of what the market actually did. The two ranges must stay disjoint or
# union() emits duplicate timestamps; keep() matches their schemas, group() collapses them
# into the one series the overrides below match by name.
PRICE_LINE = NEWEST + '''
past = from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: now())
  |> filter(fn: (r) => r._measurement == "market_price" and r._field == "market_price")
  |> keep(columns: ["_time", "_value"])

future = from(bucket: "planning")
  |> range(start: now(), stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest
                    and r._field == "price_market")
  |> keep(columns: ["_time", "_value"])

union(tables: [past, future])
  |> group()
  |> sort(columns: ["_time"])
  |> map(fn: (r) => ({ _time: r._time, "market price": r._value * 100.0 }))
  |> yield(name: "market_price")
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


def stat(id_, title, desc, query, unit, decimals, x, w, steps, color_mode="value",
         y=0, mappings=None, no_value=None):
    """A stat panel.

    `mappings` and `no_value` exist for the dispatch row (section 7.3). A stat whose value is
    a STRING cannot be coloured by thresholds, so the colour comes from value mappings
    instead -- and the base threshold step then becomes the colour of "no data", which is
    what makes `NO DISPATCHER` render red without a mapping for it.
    """
    defaults = {
        "color": {"mode": "thresholds"},
        "decimals": decimals,
        "mappings": mappings or [],
        "thresholds": {"mode": "absolute", "steps": steps},
        "unit": unit,
    }
    if no_value is not None:
        defaults["noValue"] = no_value
    return {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {
            "defaults": defaults,
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

# --- Row 1: six stats -------------------------------------------------------------------
panels.append(stat(
    1, "Plan age",
    "Time since the newest plan was made. The schedule runs every 3 hours, so anything past "
    "4 hours means a run did not fire - which is otherwise silent, because a stale plan still "
    "renders perfectly well.",
    PLAN_AGE, "s", 0, 0, 4,
    [{"color": "green", "value": None},
     {"color": "orange", "value": 12600},
     {"color": "red", "value": 14400}]))

# The two live readings below are the only panels here that come from the battery rather than
# from the plan: what it is doing right now, beside what the plan says it should be doing.
# They sit second and third, next to the plan's age, so the row reads as "is the plan fresh,
# and what is the house actually doing" before it gets to what the plan intends.
#
# `decimals` is left unset on purpose. Grafana's `watt` unit scales SI by itself, and its
# automatic decimal count then gives "1.50 kW" above a kilowatt and "850 W" below it, which
# is the wanted rendering. Pinning decimals to 2 would print "850.00 W"; pinning it to 0
# would print "2 kW".
#
# Both fields are read as the last point in the past hour rather than over the dashboard's
# range, because the dashboard's range runs into the future - the plan's horizon - and
# `last()` over that would still be the newest reading, but a range starting days back is a
# needlessly wide scan for one point. An empty panel therefore means the collector has been
# silent for an hour, which is worth seeing as blank rather than as an hours-old number.
panels.append(stat(
    9, "Battery Power now",
    "Positive is charging, negative is discharging - the same sign convention as the main "
    "AlphaESS dashboard, and the opposite of the raw `battery_power_w` field, which counts "
    "discharge as positive. Green is charging, red is discharging.",
    '''from(bucket: "alphaess")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "battery_power_w")
  |> last()
  |> map(fn: (r) => ({ r with _value: -r._value }))
  |> yield(name: "battery now")
''', "watt", None, 4, 4,
    [{"color": "red", "value": None}, {"color": "green", "value": 0}]))

panels.append(stat(
    10, "Grid Power now",
    "Positive is drawing from the grid, negative is returning to it. Around zero means the "
    "house is running off solar and battery.",
    '''from(bucket: "alphaess")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "grid_power_w")
  |> last()
  |> yield(name: "grid now")
''', "watt", None, 8, 4,
    [{"color": "text", "value": None}]))

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
''', "currencyEUR", 2, 12, 4,
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
''', "percent", 0, 16, 4,
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
''', "percent", 0, 20, 4,
    [{"color": "text", "value": None}]))

# --- Row 2: what the dispatcher is commanding -------------------------------------------
#
# Section 7. Kuma answers "is it broken"; it cannot answer "what is the battery being told to
# do right now, and why" -- a question asked from a phone, in the kitchen, watching the meter.
# The row above shows the PLAN and the row below shows the ACTUAL; this is the missing middle
# term, the command that is supposed to turn one into the other.
#
# STALENESS IS THE FAILURE MODE THESE ARE DESIGNED AGAINST. A dead dispatcher does not clear
# `dispatch_state` -- it leaves the last point sitting there forever, and a panel querying a
# wide range with `last()` would cheerfully render a command that expired fifty minutes ago
# as the current state of the battery. That is worse than a blank panel: it is a confident
# wrong answer in the one place you go to check. Hence `range(start: -5m)` on every query
# here, five loop iterations, past which the panels go to No data on purpose.
DISPATCH_LAST = '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "%s")
  |> last()
'''

# `Released - following house` and `NO DISPATCHER` describe the SAME register contents:
# start=0. Only the freshness of the point separates them, which is why the short window
# above is not optional. The base threshold step is red because it is also the colour Grafana
# uses for `noValue` -- so a missing point renders as a loud red NO DISPATCHER rather than an
# empty cell, while the mappings below colour the states that were actually commanded.
panels.append(stat(
    20, "Dispatch state",
    "What the dispatcher last told the inverter to do, decoded. NO DISPATCHER in red means "
    "no point has arrived for five minutes: the loop is down, and whatever the registers "
    "still hold is not being refreshed. Note that a released dispatch and a dead dispatcher "
    "look identical at the register level - only the freshness of this point tells them "
    "apart.",
    DISPATCH_LAST % "action", "none", None, 0, 6,
    [{"color": "red", "value": None}],
    y=4, no_value="NO DISPATCHER",
    mappings=[{"type": "value", "options": {
        "charging from grid": {"color": "green", "index": 0},
        "charging from PV": {"color": "green", "index": 1},
        "discharging to grid": {"color": "orange", "index": 2},
        "hold (battery frozen)": {"color": "blue", "index": 3},
        "self-consumption (released)": {"color": "text", "index": 4},
        "no dispatch": {"color": "text", "index": 5},
        # Not something this dispatcher commands -- it holds with Mode 3. Seeing it means
        # the app is driving the block, so it gets the warning colour rather than a
        # neutral one.
        "active at 0 W": {"color": "yellow", "index": 6},
    }}]))

# Charging positive, discharging negative - the same convention as "Battery Power now" four
# panels up, and the same green/red. The dispatcher flips the register's sign once, at the
# encoder, so nothing has to be negated here. `decimals` stays unset for the reason the
# comment on that panel gives: `watt` self-scales, and pinning decimals gives either
# "850.00 W" or "2 kW".
panels.append(stat(
    21, "Commanded power",
    "The setpoint written to 0x0881, in the same sign convention as Battery Power now: "
    "positive charging, negative discharging. Compare the two - a large gap between "
    "commanded and actual means the inverter accepted the command and did not honour it.",
    DISPATCH_LAST % "setpoint_w", "watt", None, 6, 6,
    [{"color": "red", "value": None}, {"color": "green", "value": 0}],
    y=4))

panels.append(stat(
    22, "Commanded target SoC",
    "Where the current command is driving the battery (0x0886). Only meaningful while a "
    "Mode 2 command is live - a hold writes no target, so this holds whatever was last set.",
    DISPATCH_LAST % "target_soc_pct", "percent", 0, 12, 6,
    [{"color": "text", "value": None}],
    y=4))

# Counting down from the dispatcher's own `expires_at` rather than from the inverter's
# duration register, which section 5.1 records counting down erratically - observed reading
# 300 s three times across two minutes, then straight to expiry.
#
# GATED ON dispatch_active, AND ON BOTH BEING WRITTEN AT THE SAME INSTANT. `expires_at` is a
# conditional field: state.py writes it only while a command is live, so after a normal
# release the last one sits in the window for another five minutes and `last()` happily
# returns it. Counting that down turns this panel red and accuses a perfectly healthy
# dispatcher of having stopped, every single time it releases.
#
# `exists` on both columns is what tells the two cases apart, and it has to be a same-instant
# test rather than a plain `dispatch_active != 0`: every field in a point shares one
# timestamp, so an inner match means "this tick wrote both" - a live command. A release wrote
# dispatch_active without expires_at, so the pivot puts them on different rows and neither has
# both. Crucially this KEEPS the case the panel exists for: a loop that stops mid-command
# leaves both fields stale at the same instant, they still pivot onto one row, and the
# countdown drains to zero and goes red exactly as it should.
panels.append(stat(
    23, "Command expires in",
    "Time left on the dead man's switch. The loop rewrites it every 60 s, so this should sit "
    "near 5 minutes and never fall far; dropping toward zero means the loop has stopped "
    "refreshing and the inverter is about to revert to self-consumption on its own. Blank "
    "means no command is live, which is a normal resting state, not a fault.",
    '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state"
                   and (r._field == "expires_at" or r._field == "dispatch_active"))
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> filter(fn: (r) => exists r.expires_at and exists r.dispatch_active
                   and r.dispatch_active != 0)
  |> map(fn: (r) => ({ _time: r._time, _value:
       float(v: r.expires_at) - float(v: int(v: now())) / 1000000000.0 }))
  |> yield(name: "expires in")
''', "s", 0, 18, 6,
    [{"color": "red", "value": None}, {"color": "green", "value": 60}],
    y=4))

# --- Panel: the register decode table ---------------------------------------------------
#
# The literal answer to "human-readable instead of register values" -- but the raw column
# stays. Half the value of this table is being able to check a decode against the spec
# without leaving the dashboard, and every encoding in section 5.2 was got wrong by somebody
# first: the community's 0.392 %/bit claim is in the wild precisely because nobody could see
# both columns at once.
#
# Built as a union of one-row streams rather than with findRecord, so that a stale window
# yields an empty table and Grafana's own "No data" rather than a Flux error. The 32-bit
# values are recombined here because the point stores the words verbatim, one field each.
# THE FIELD LIST IS EXPLICIT, and every name on it is one state.py writes on EVERY tick.
# That is a correctness requirement, not tidiness. `last()` returns the newest point per
# field, each carrying its own timestamp, and `pivot` keys rows by that timestamp -- so the
# moment one conditional field (`expires_at`, `slot_start`, `slot_action`, `plan_run`) goes
# stale while the rest keep being written, the pivot emits TWO rows at two different instants
# and the union below renders the whole table twice: one populated copy and one blank, for
# the five minutes until the stale field falls out of the window. Adding a conditional field
# to this list brings that straight back. tests/test_dispatch_dashboard.py pins it.
DECODE_TABLE = '''base = from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state")
  |> filter(fn: (r) => r._field == "dispatch_active" or r._field == "setpoint_w"
                    or r._field == "action" or r._field == "mode_name"
                    or r._field == "target_soc_pct" or r._field == "duration_s"
                    or r._field =~ /^raw_08[0-9a-f][0-9a-f]$/)
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")

union(tables: [
  base |> map(fn: (r) => ({
    register: "0x0880", name: "Dispatch start", raw: r.raw_0880,
    means: if r.dispatch_active != 0 then "Active" else "Released"
  })),
  base |> map(fn: (r) => ({
    register: "0x0881", name: "Active power", raw: r.raw_0881 * 65536 + r.raw_0882,
    means: string(v: r.setpoint_w) + " W - " + r.action
  })),
  base |> map(fn: (r) => ({
    register: "0x0885", name: "Mode", raw: r.raw_0885,
    means: r.mode_name
  })),
  base |> map(fn: (r) => ({
    register: "0x0886", name: "SoC target", raw: r.raw_0886,
    means: string(v: r.target_soc_pct) + " %"
  })),
  base |> map(fn: (r) => ({
    register: "0x0887", name: "Duration", raw: r.raw_0887 * 65536 + r.raw_0888,
    means: string(v: r.duration_s / 60) + " min " + string(v: r.duration_s % 60) + " s"
  })),
])
  |> group()
  |> sort(columns: ["register"])
  |> yield(name: "decode")
'''

panels.append({
    "datasource": DS,
    "description": "The dispatch block as it reads right now, decoded. The raw column is "
                   "kept deliberately: it is what lets a decode be checked against the "
                   "AlphaESS register spec without leaving this page, and every encoding "
                   "here was got wrong by somebody first. 0x0881 and 0x0887 are 32-bit and "
                   "are shown recombined from their two words. 0x0883 is reactive power and "
                   "is never written - it is not in this table because the dispatcher does "
                   "not touch it. Empty means no point in five minutes: see Dispatch state.",
    "fieldConfig": {
        "defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [
            {"matcher": {"id": "byName", "options": "register"},
             "properties": [{"id": "displayName", "value": "Register"}]},
            {"matcher": {"id": "byName", "options": "name"},
             "properties": [{"id": "displayName", "value": "Name"}]},
            {"matcher": {"id": "byName", "options": "raw"},
             "properties": [{"id": "displayName", "value": "Raw"},
                            {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "means"},
             "properties": [{"id": "displayName", "value": "Means"}]},
        ],
    },
    "gridPos": {"h": 6, "w": 24, "x": 0, "y": 8},
    "id": 24,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [],
    },
    "pluginVersion": "11.6.0",
    # Column order fixed here rather than by the order of fields in the Flux `map`: Flux does
    # not promise an output column order, so a map that happens to come out right today would
    # reorder itself on an unrelated change and nobody would notice.
    "transformations": [{
        "id": "organize",
        "options": {
            "excludeByName": {},
            "includeByName": {},
            "renameByName": {},
            "indexByName": {"register": 0, "name": 1, "raw": 2, "means": 3},
        },
    }],
    "targets": [target(DECODE_TABLE)],
    "title": "Dispatch registers, decoded",
    "type": "table",
})

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
''', "C"),
     # The third series is what closes the loop visually. Plan says 78 %, dispatcher
     # commanded 78 %, battery reached 71 %: the gap between the second and third lines is
     # DELIVERY error, the gap between the first and second is a DISPATCHER bug. Those are
     # different problems with different fixes, and without this line the chart cannot tell
     # them apart.
     #
     # Restricted to live Mode 2 commands. 0x0886 keeps its last value through a hold and
     # through a release -- a Mode 3 hold writes no target at all -- so plotting the register
     # unconditionally would draw a confident flat line at a target nothing is driving
     # toward. Requires dispatch_active and mode alongside it, hence the pivot.
     #
     # NULLED, NOT FILTERED, and that is the whole difference between this working and it
     # doing the exact thing the paragraph above says it prevents. Dropping the rows leaves
     # the series with no datapoints across an idle stretch, and a stepAfter line with no
     # datapoints simply joins the two ends -- redrawing the confident flat line, now with a
     # comment claiming it cannot happen. An explicit null is a datapoint that says "nothing
     # commanded here", which is what Grafana needs to break the line.
     target('''import "internal/debug"

from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "dispatch_state")
  |> filter(fn: (r) => r._field == "target_soc_pct" or r._field == "mode"
                    or r._field == "dispatch_active")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ _time: r._time, "commanded":
       if r.dispatch_active != 0 and r.mode == 2 then float(v: r.target_soc_pct)
       else debug.null(type: "float") }))
  |> yield(name: "commanded")
''', "D")],
    14, 9, "percent",
    [series_override("planned", [{"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}}]),
     series_override("actual", [{"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                                {"id": "custom.fillOpacity", "value": 0}]),
     series_override("commanded", [{"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
                                   {"id": "custom.fillOpacity", "value": 0},
                                   {"id": "custom.lineStyle",
                                    "value": {"dash": [6, 4], "fill": "dash"}}]),
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
    "band - see the table below for when to change them. Ahead of now the price is the "
    "plan's own - the prices it was optimised against; behind now it is the price this "
    "collector stored at the time. A step at now means the two feeds disagree there.",
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
     target(PRICE_LINE, "B"),
     target(threshold_line("sell", "sell above"), "C"),
     target(threshold_line("buy", "buy below"), "D")],
    23, 9, "kwatth",
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

# --- Panel: dispatch action table ---------------------------------------------------------
# The SoC line on panel 5 cannot answer "will this interval export?". It is a step chart, so
# every 15-minute segment is flat by construction and a single flat step means nothing; only a
# RUN of steps at one level is a hold. Worse, a rising step is `charge` or `self` and the line
# cannot tell them apart, because the difference is `import_wh`, which is not plotted anywhere.
# This table runs the classifier instead of asking the eye to infer it.
#
# It is a second implementation of dispatch/translator.py:classify, which is a real cost: the
# two can drift. The alternative was worse. slots.json lives on a volume inside the dispatch
# container and is not in InfluxDB, so Grafana cannot read the translator's own output, and
# when the container is down -- which is exactly when you want to know what it WOULD do --
# there is nothing to read at all. The floors below are ENERGY_FLOOR_WH, SURPLUS_FLOOR_WH and
# FULL_TOLERANCE_WH; test_grafana_provisioning.py pins them against the translator's copies.
#
# What this deliberately does NOT model is slots.py:decide, which re-checks each target
# against LIVE SoC and downgrades a charge whose target already sits below it to a hold. So
# this is the plan's intent, not a prediction of the command. The gap between the two is the
# drift this whole dashboard exists to show.
DISPATCH_ACTIONS = NEWEST + '''
from(bucket: "planning")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "plan" and r.plan_run == newest)
  |> filter(fn: (r) => r._field == "charge_wh" or r._field == "discharge_wh"
                    or r._field == "soc_wh" or r._field == "import_wh"
                    or r._field == "export_wh" or r._field == "pv_forecast_wh")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> map(fn: (r) => ({
      _time: r._time,
      action:
        if r.charge_wh > 50.0 and r.discharge_wh > 50.0 then
          "plan error -- charge and discharge in one interval"
        else if r.discharge_wh > 50.0 then
          (if r.export_wh > 50.0 then "discharge -- sells to grid"
           else "self -- battery covers load")
        else if r.charge_wh > 50.0 then
          (if r.import_wh > 50.0 then "charge -- buys from grid"
           else "self -- soaks up solar")
        else if r.soc_wh >= float(v: ${capacity_wh}) - 50.0 and r.import_wh <= 10.0
             and (r.export_wh > 10.0 or r.pv_forecast_wh > 10.0) then
          "self -- full, still harvesting"
        else
          "hold -- surplus goes to grid",
      soc_pct: r.soc_wh / float(v: ${capacity_wh}) * 100.0,
      charge_wh: r.charge_wh,
      import_wh: r.import_wh,
      export_wh: r.export_wh
    }))
  |> sort(columns: ["_time"])
  |> yield(name: "dispatch actions")
'''

panels.append({
    "datasource": DS,
    "description": "What the dispatcher would do in each interval, worked out from the plan "
                   "the same way dispatch/translator.py works it out. Read it when the SoC "
                   "graph above is ambiguous, which is most of the time: that graph steps, so "
                   "a flat segment is just one interval and tells you nothing, and a rising "
                   "one could be either 'charge' or 'self'. "
                   "'hold' is the row to look for - the battery is frozen at 0 W and any "
                   "solar beyond the house load is exported, whatever the price. A run of "
                   "'hold' rows through the afternoon is surplus being given away. "
                   "'self' means the battery is simply released to self-consumption, either "
                   "because the plan moves energy without touching the grid or because it is "
                   "full and would otherwise spill. "
                   "This is the plan's intent only. The live dispatcher checks each target "
                   "against the battery's actual SoC and quietly turns a charge it has "
                   "already overshot into a hold, so when actual SoC has drifted above plan "
                   "you will get more holds than are listed here.",
    "fieldConfig": {
        "defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        # Explicit widths on every column. Without them Grafana divides the panel evenly and
        # a four-character number gets an eighth of a 24-cell dashboard, which pushes the
        # only column worth reading -- the action -- into a narrow, wrapping strip.
        "overrides": [
            # Same reasoning as the settings table: the year repeats on every row of a
            # 36-hour horizon and the seconds are always :00.
            {"matcher": {"id": "byName", "options": "_time"},
             "properties": [{"id": "displayName", "value": "Time"},
                            {"id": "unit", "value": "time:MM-DD HH:mm"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "action"},
             "properties": [{"id": "displayName", "value": "What dispatch does"},
                            {"id": "custom.width", "value": 300}]},
            {"matcher": {"id": "byName", "options": "soc_pct"},
             "properties": [{"id": "decimals", "value": 1}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "SoC after"},
                            {"id": "custom.width", "value": 100}]},
            {"matcher": {"id": "byName", "options": "charge_wh"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "watth"},
                            {"id": "displayName", "value": "Charge"},
                            {"id": "custom.width", "value": 90}]},
            {"matcher": {"id": "byName", "options": "import_wh"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "watth"},
                            {"id": "displayName", "value": "Import"},
                            {"id": "custom.width", "value": 90}]},
            {"matcher": {"id": "byName", "options": "export_wh"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "watth"},
                            {"id": "displayName", "value": "Export"},
                            {"id": "custom.width", "value": 90}]},
        ],
    },
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": 32},
    "id": 11,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": False, "displayName": "Time"}],
    },
    "pluginVersion": "11.6.0",
    # Flux does not promise an output column order, so the order is pinned here rather than
    # left to the order of the fields in the `map` above. Same reason as panel 7.
    "transformations": [{
        "id": "organize",
        "options": {
            "excludeByName": {},
            "includeByName": {},
            "renameByName": {},
            "indexByName": {"_time": 0, "action": 1, "soc_pct": 2, "charge_wh": 3,
                            "import_wh": 4, "export_wh": 5},
        },
    }],
    "targets": [target(DISPATCH_ACTIONS)],
    "title": "What dispatch would do, interval by interval",
    "type": "table",
})

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
        # Every column carries an explicit width. Grafana's default is to divide the panel
        # evenly, which on a 24-cell row gives a three-character "Exact" the same space as a
        # timestamp -- and the columns are the first thing to get squeezed on a phone, which
        # is where this table is actually read.
        "overrides": [
            # Month-day and hour-minute only. The year is the same on every row of a
            # 36-hour horizon and the seconds are always :00 -- both are width spent on
            # nothing.
            {"matcher": {"id": "byName", "options": "from_t"},
             "properties": [{"id": "displayName", "value": "From"},
                            {"id": "unit", "value": "time:MM-DD HH:mm"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "until_t"},
             "properties": [{"id": "displayName", "value": "Until"},
                            {"id": "unit", "value": "time:MM-DD HH:mm"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "action"},
             "properties": [{"id": "displayName", "value": "Set"},
                            {"id": "custom.width", "value": 90}]},
            {"matcher": {"id": "byName", "options": "set_to"},
             "properties": [{"id": "decimals", "value": 2},
                            {"id": "displayName", "value": "to ct/kWh"},
                            {"id": "custom.width", "value": 100}]},
            {"matcher": {"id": "byName", "options": "target_soc"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "Target SoC"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "exact"},
             "properties": [{"id": "displayName", "value": "Exact"},
                            {"id": "custom.width", "value": 70}]},
        ],
    },
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 42},
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
        # Widths on every column, for the same reason as the settings table above. The time
        # column is also cut to month-day and hour-minute: a column cannot be "only as wide
        # as it needs to be" while it renders a full ISO timestamp, so narrowing it and
        # shortening the format are one change, not two.
        "overrides": [
            {"matcher": {"id": "byName", "options": "_time"},
             "properties": [{"id": "displayName", "value": "Time"},
                            {"id": "unit", "value": "time:MM-DD HH:mm"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "action"},
             "properties": [{"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "kWh"},
             "properties": [{"id": "decimals", "value": 2},
                            {"id": "custom.width", "value": 90}]},
            {"matcher": {"id": "byName", "options": "soc_pct"},
             "properties": [{"id": "decimals", "value": 0}, {"id": "unit", "value": "percent"},
                            {"id": "displayName", "value": "SoC after"},
                            {"id": "custom.width", "value": 110}]},
            {"matcher": {"id": "byName", "options": "ct_kWh"},
             "properties": [{"id": "decimals", "value": 1},
                            {"id": "displayName", "value": "ct/kWh"},
                            {"id": "custom.width", "value": 90}]},
        ],
    },
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": 50},
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
    "title": "Planned Actions in app",
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
    "description": "What the planner intended, from the planning bucket; what the "
                   "dispatcher commanded, from dispatch_state; and what the battery "
                   "actually did. Reads top to bottom as plan, command, actual - the gap "
                   "between plan and command is a dispatcher bug, the gap between command "
                   "and actual is delivery error.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "links": [],
    "panels": panels,
    "preload": False,
    "refresh": "5m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "plan"],
    "templating": {"list": [CAPACITY_VAR]},
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
    "title": "Battery Plan",
    "uid": "alphaess-battery-plan",
    # BUMP THIS on every change below. Grafana's file provisioner keeps the dashboard it
    # already stored unless the incoming version is higher - it reads the new file, compares,
    # and does nothing, with no error and no log line. Restarting or recreating the container
    # does not help. The symptom is a fix that appears not to have worked, which sends you
    # back to re-debug a query that was already correct.
    # 2: series renamed out of _value so the byName overrides bind.
    # 3: price line switched to raw market price; sell/buy threshold lines added.
    # 8: price line reads the plan's own price_market ahead of now, so tomorrow's half of the
    #    horizon is no longer blank until refresh-prices.sh next runs.
    # 9: From/Until in the settings table drop the year and the seconds.
    # 10: dispatch row (section 7) - four command stats, the register decode table, and the
    #     commanded-SoC series on panel 5. Everything below panel 5 moves down 10 rows.
    # 11: capacity_wh becomes a query variable reading the plan, replacing the 27900 textbox.
    # 12: that query picks by parsed plan_run over a -14d/72h window, not by the last row of
    #     every run - runs share a horizon end, so sorting on _time tied across all of them.
    # 13: "What dispatch would do" table (panel 11), below the price panel. The two tables
    #     under it move down 10 rows.
    # 14: title drops the "AlphaESS" prefix, as every dashboard here did. The uid is
    #     unchanged, so /d/alphaess-battery-plan and every link to it still resolve.
    # 15: both tables get explicit column widths and a short time format, for reading on a
    #     phone; "Planned actions" renamed to "Planned Actions in app".
    "version": 15,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
