"""Generates grafana/alphaess-battery-health.json -- the Battery Health dashboard.

    python grafana/generate-battery-health.py grafana/alphaess-battery-health.json

EDIT THIS FILE, NEVER THE JSON. `tests/test_grafana_provisioning.py` re-runs every generator
and diffs the result, so a hand-edit to the output fails the suite by design.

WHY A THIRD BATTERY DASHBOARD. Battery Plan answers "is the optimiser doing the right thing",
Dispatch answers "is the control loop doing what the plan says right now". Neither answers
"is the battery itself okay" -- a slower question, about degradation and faults rather than
about today's price curve, and this dashboard is scoped to it alone. It has no future horizon:
`"time"` runs back thirty days, not forward, because there is nothing here to plan.

SCOPE IS SMALLER THAN THE ORIGINAL HANDOVER ASKED FOR, on purpose. The health-poller backend
(#129) only ever shipped what had unambiguous register addressing: cell temperature (already
existed), the fault/warning block as raw hex, the already-read power limits republished under
health-dashboard field names, and the weekly firmware/inverter-firmware/system-config blocks as
raw hex. Cell voltage, SoH, remaining time, daily energy and lifetime cycles are all still
unconfirmed register layouts (TODO.md items 12-13) and `dispatch/state.py` never publishes
them. A panel querying a field that is never written is not "no data yet", it is the exact
mistake `tests/test_dispatch_dashboard.py::TestFieldContract::test_every_field_filter_names_a_field_we_publish`
exists to catch -- so those panels are deferred to the same follow-up PR that adds their
backend fields, rather than shipped now pointed at names nothing writes.

STALENESS WINDOWS ARE CADENCE-MATCHED, not one constant for the whole page. Section 7.3's
original argument (`generate-dispatch.py`) was written for tick-cadence fields alone: a dead
loop leaves its last point sitting in `dispatch_state` forever, so a `last()` window has to be
tight enough that an old point ages out rather than reading as current. The health-poller's
hourly and weekly fields need the same protection at their own cadence -- a `-5m` window on an
hourly field would show "no data" for fifty-nine minutes out of every sixty on a perfectly
healthy poller. `tests/test_dispatch_dashboard.py`'s `_allowed_last_windows` pins this per
field; matching it here requires an explicit `or`-chain of literal `_field == "..."` filters
for the raw-hex tables below, not a regex -- the guard only recognises a field's cadence from a
literal equality, and a `=~` filter would silently fall back to the tight tick-cadence window
and reject a table's own -3h/-10d range outright.
"""
import json, sys

DS = {"type": "influxdb", "uid": "${DS_ALPHAESS}"}

# --- Cadence-matched "last known value" queries ------------------------------------------
#
# Three separate constants, not one shared with a variable window: each one's window is a
# fact about the field it reads, and `tests/test_dispatch_dashboard.py::TestAllowedLastWindows`
# pins the mapping from field name to window independently of this file. Named `HEALTH_LAST_*`
# rather than `DISPATCH_LAST` so this file is not swept into
# `TestTheGeneratorsAgree::test_the_dispatch_query_constant_is_identical_everywhere`, which
# checks that literal name across generators -- these constants read a different cadence and
# have no reason to be pinned equal to it.
HEALTH_LAST_TICK = '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "%s")
  |> last()
'''

# `dispatch/scheduler.py` step 8c, `HEALTH_REFRESH_S = 3600`. Comfortably wider than the
# cadence it guards, same argument as the tick window above, just scaled.
HEALTH_LAST_HOURLY = '''from(bucket: "alphaess")
  |> range(start: -3h)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "%s")
  |> last()
'''


# --- Raw-hex block addressing, hand-copied from dispatch/registers.py --------------------
#
# COPIED, NOT IMPORTED. This script has no dependency on the `dispatch` package -- same trade
# `generate-battery-plan.py`'s `SHORTFALL_PCT` comment names: these have to be kept in sync by
# hand against `registers.py`'s `FAULT_BLOCK`/`FIRMWARE_BLOCK`/`INVERTER_FW_BLOCK`/
# `SYSTEM_CONFIG_BLOCK`, and a future change to one of those base addresses or word counts has
# to be mirrored here.
FAULT_FIELDS = [f"fault_raw_{0x0131 + i:04x}" for i in range(22)]
FIRMWARE_FIELDS = [f"firmware_raw_{0x0115 + i:04x}" for i in range(6)]
INVERTER_FW_FIELDS = [f"inverter_fw_raw_{0x0640 + i:04x}" for i in range(20)]
SYSTEM_CONFIG_FIELDS = [f"system_config_raw_{0x0800 + i:04x}" for i in range(16)]


def _field_filter(fields):
    return " or ".join(f'r._field == "{f}"' for f in fields)


def raw_table_query(window, fields, prefix, yield_name):
    """One row per register in a hex-keyed raw block: `register` (its own hex address,
    reconstructed from the field name) and `value` (the word, decimal).

    An explicit `or`-chain of literal `_field == "..."` conditions rather than
    `r._field =~ /^prefix_[0-9a-f]{4}$/` -- see the module docstring for why a regex would
    silently defeat `TestStalenessGuards.test_every_last_value_query_uses_a_short_window`.
    """
    return '''import "strings"

from(bucket: "alphaess")
  |> range(start: %s)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and (%s))
  |> last()
  |> map(fn: (r) => ({ _time: r._time,
       register: "0x" + strings.trimPrefix(v: r._field, prefix: "%s"),
       value: r._value }))
  |> keep(columns: ["register", "value"])
  |> group()
  |> sort(columns: ["register"])
  |> yield(name: "%s")
''' % (window, _field_filter(fields), prefix, yield_name)


# The republished power-limit timeseries. NAMED COLUMNS, not `_value` -- Grafana names a field
# after its column but special-cases `_value` to the literal "Value", so two series both
# yielding `_value` would arrive as two fields both called "Value" and every `byName` override
# would miss. Seen on the NAS 2026-07-30 (generate-dispatch.py's own note on the same trap).
LIMITS_HISTORY = '''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "dispatch_state"
                   and (r._field == "max_charge_power_w" or r._field == "max_discharge_power_w"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ _time: r._time,
       "max charge": r.max_charge_power_w, "max discharge": r.max_discharge_power_w }))
  |> yield(name: "limits")
'''


# --- Panel helpers, copied from generate-dispatch.py / generate-battery-plan.py ----------
#
# COPIED, NOT SHARED, and deliberately -- the house rule `generate-battery-score.py:29`
# states: tests pin every dashboard to the same query instead of sharing a module, which
# catches drift at the only place it could do harm.


def target(query, ref="A"):
    return {"datasource": DS, "query": query, "refId": ref}


def stat(id_, title, desc, query, unit, decimals, x, w, steps, color_mode="value",
         y=0, mappings=None, no_value=None):
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
        "fieldConfig": {"defaults": defaults, "overrides": []},
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


def series_override(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def timeseries(id_, title, desc, targets, x, y, w, h, unit, overrides, fill=10,
               style="line"):
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
                    "lineInterpolation": "linear",
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
            "legend": {"calcs": [], "displayMode": "list", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"},
        },
        "pluginVersion": "11.6.0",
        "targets": targets,
        "title": title,
        "type": "timeseries",
    }


def raw_table(id_, title, desc, query, x, y, w, h):
    """A register/value table for a raw-hex block. Same shape as `generate-dispatch.py`'s
    decode table, minus the "means" column -- there is nothing decoded to put in it yet."""
    return {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "register"},
                 "properties": [{"id": "displayName", "value": "Register"},
                                {"id": "custom.width", "value": 110}]},
                {"matcher": {"id": "byName", "options": "value"},
                 "properties": [{"id": "displayName", "value": "Value"},
                                {"id": "decimals", "value": 0}]},
            ],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": id_,
        "options": {
            "cellHeight": "sm",
            "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
            "showHeader": True,
        },
        "pluginVersion": "11.6.0",
        # Flux does not promise an output column order -- same reason every other generated
        # table here pins one.
        "transformations": [{
            "id": "organize",
            "options": {
                "excludeByName": {}, "includeByName": {}, "renameByName": {},
                "indexByName": {"register": 0, "value": 1},
            },
        }],
        "targets": [target(query)],
        "title": title,
        "type": "table",
    }


panels = []

# =========================================================================================
# Row 1, y=0 -- where things stand right now
# =========================================================================================
panels.append(stat(
    1, "SoC now",
    "The battery level the dispatcher last read, same field the Dispatch dashboard shows.",
    HEALTH_LAST_TICK % "soc_pct", "percent", 1, 0, 5,
    [{"color": "text", "value": None}],
    no_value="unreadable"))

# Thresholds copied verbatim from alphaess-dashboard.json's "Min cell temp"/"Max cell temp" --
# the min tile keeps its own cold band (a lithium pack near freezing derates or refuses a
# charge) rather than sharing the max tile's hot-side ladder, same reasoning that panel's own
# description gives.
panels.append(stat(
    2, "Min cell temp",
    "The coldest cell across the whole battery, tagged with which pack it is in. A lithium "
    "pack near freezing refuses or derates a charge, which is why this tile keeps its own "
    "cold band rather than sharing the max tile's ladder.",
    HEALTH_LAST_TICK % "min_cell_temp_c", "celsius", 1, 5, 5,
    [{"color": "blue", "value": None}, {"color": "green", "value": 5.0},
     {"color": "orange", "value": 35.0}, {"color": "red", "value": 45.0}],
    no_value="unreadable"))

panels.append(stat(
    3, "Max cell temp",
    "The hottest cell across the whole battery, tagged with which pack it is in.",
    HEALTH_LAST_TICK % "max_cell_temp_c", "celsius", 1, 10, 5,
    [{"color": "green", "value": None}, {"color": "orange", "value": 35.0},
     {"color": "red", "value": 45.0}],
    no_value="unreadable"))

panels.append(stat(
    4, "Max charge power limit",
    "The inverter's own charge ceiling, read hourly and republished here under the health "
    "dashboard's field names -- the same reading `slots.clamp` already uses to cap a command, "
    "not a fresh register read.",
    HEALTH_LAST_HOURLY % "max_charge_power_w", "watt", None, 15, 5,
    [{"color": "text", "value": None}],
    no_value="unreadable"))

panels.append(stat(
    5, "Max discharge power limit",
    "The inverter's own discharge ceiling, read hourly and republished under the health "
    "dashboard's field names. Independent of the charge limit above -- one being readable "
    "does not imply the other is.",
    HEALTH_LAST_HOURLY % "max_discharge_power_w", "watt", None, 20, 4,
    [{"color": "text", "value": None}],
    no_value="unreadable"))

# =========================================================================================
# Row 2, y=4 -- the week's thermal story
# =========================================================================================
#
# DUPLICATED VERBATIM from alphaess-dashboard.json's panel id 30, query and series overrides
# both. `tests/test_grafana_provisioning.py::test_copied_panels_match_their_source` holds this
# copy to its source; unlike every other entry in `COPIED_PANELS` there, the source here is the
# hand-maintained alphaess-dashboard.json and this generated file is the copy -- the direction
# is reversed because this is the first panel that started life on a hand-maintained dashboard
# rather than a generated one.
panels.append({
    "datasource": DS,
    "description": "Seven days of the coldest and hottest cell. The two tiles above say where "
                   "the battery is now; this says what it has been doing, which is the only "
                   "way to tell a warm afternoon from a pack that has been climbing all week. "
                   "The band between the lines is the spread across the fleet - it widens "
                   "under load and closes overnight, and a spread that stops closing is worth "
                   "looking into.",
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
                "drawStyle": "line",
                "fillOpacity": 0,
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
            "unit": "celsius",
        },
        "overrides": [
            series_override("min_cell_temp_c", [
                {"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}},
                {"id": "custom.lineInterpolation", "value": "linear"},
                {"id": "displayName", "value": "min cell"}]),
            series_override("max_cell_temp_c", [
                {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                {"id": "custom.lineInterpolation", "value": "linear"},
                {"id": "displayName", "value": "max cell"}]),
        ],
    },
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 4},
    "id": 6,
    "options": {
        "legend": {"calcs": [], "displayMode": "list", "placement": "bottom",
                   "showLegend": True},
        "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"},
    },
    "pluginVersion": "11.6.0",
    "targets": [target('''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "dispatch_state"
                   and (r._field == "min_cell_temp_c" or r._field == "max_cell_temp_c"))
  |> yield(name: "cell temps")
''')],
    "timeFrom": "7d",
    "title": "Battery cell temperature (min/max)",
    "type": "timeseries",
})

# =========================================================================================
# Row 3, y=12 -- the inverter's own ceilings, over time
# =========================================================================================
panels.append(timeseries(
    7, "Charge / discharge power limits",
    "The inverter's own charge/discharge ceilings, read hourly. A limit that steps down over "
    "time -- rather than jumping back up on the next hourly read -- is the inverter derating "
    "itself, which is worth noticing well before it shows up as a shortfall on the Dispatch "
    "dashboard.",
    [target(LIMITS_HISTORY)], 0, 12, 24, 8, "watt",
    [series_override("max charge", [
        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}]),
     series_override("max discharge", [
        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}])],
    fill=0))

# =========================================================================================
# Row 4, y=20 -- faults and warnings, raw
# =========================================================================================
#
# RAW WORDS ONLY. `registers.py`'s FAULT_BLOCK comment, and TODO.md item 15, explain why: a
# naive "count the nonzero words" summary would pin itself permanently active if even one word
# in the range turns out not to be a fault/warning bit, which is worse than no summary. This
# table is the same "checkable, not decoded" treatment the block gets in `dispatch_state`
# itself -- read it against the AlphaESS app's own fault/warning display.
panels.append(raw_table(
    8, "Faults and warnings (raw)",
    "FAULT_BLOCK, 0x0131-0x0146, read hourly. No word here is confirmed to be a fault bit "
    "specifically rather than a counter or status value (TODO.md item 15), so there is no "
    "derived 'faults active' count -- only the raw words, to be checked against the AlphaESS "
    "app's own display.",
    raw_table_query("-3h", FAULT_FIELDS, "fault_raw_", "faults"),
    0, 20, 24, 10))

# =========================================================================================
# Row 5, y=30 -- weekly tripwires, raw
# =========================================================================================
#
# -10D RANGE, matching the poller's weekly gate (`WEEKLY_HEALTH_REFRESH_S`,
# `scheduler.py` step 8d) -- a `-3h` window here would show "no data" for six days out of
# seven on a perfectly healthy poller. A tripwire, not a trend: these three tables exist to
# notice a firmware version or a config word CHANGING between two reads, not to chart one.
panels.append(raw_table(
    9, "Firmware versions (raw)",
    "FIRMWARE_BLOCK, 0x0115-0x011A (BMU/LMU/ISO firmware, battery capacity/type), read "
    "weekly. Which word is which named value is not confirmed anywhere in this repo yet "
    "(TODO.md item 14), so this is the raw words rather than named fields.",
    raw_table_query("-10d", FIRMWARE_FIELDS, "firmware_raw_", "firmware"),
    0, 30, 8, 10))

panels.append(raw_table(
    10, "Inverter firmware (raw)",
    "INVERTER_FW_BLOCK, 0x0640-0x0653 (inverter master/slave firmware and serial), read "
    "weekly. Same raw-hex treatment as Firmware versions, and for the same reason.",
    raw_table_query("-10d", INVERTER_FW_FIELDS, "inverter_fw_raw_", "inverter_fw"),
    8, 30, 8, 10))

panels.append(raw_table(
    11, "System config (raw)",
    "SYSTEM_CONFIG_BLOCK, 0x0800-0x080F (max feed-into-grid %, PV capacity settings, system "
    "mode, battery-ready flag), read weekly. Same raw-hex treatment, and for the same reason.",
    raw_table_query("-10d", SYSTEM_CONFIG_FIELDS, "system_config_raw_", "system_config"),
    16, 30, 8, 10))


dashboard = {
    "__inputs": [{
        "description": "",
        "label": "alphaess",
        "name": "DS_ALPHAESS",
        "pluginId": "influxdb",
        "pluginName": "InfluxDB",
        "type": "datasource",
    }],
    "__elements": {},
    "__requires": [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "11.6.0"},
        {"type": "datasource", "id": "influxdb", "name": "InfluxDB", "version": "1.0.0"},
        {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
        {"type": "panel", "id": "table", "name": "Table", "version": ""},
        {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
    ],
    "annotations": {"list": []},
    "description": "Is the battery itself okay -- faults, thermal history and the inverter's "
                   "own power ceilings, on the cadence each actually updates. Battery Plan "
                   "answers what the optimiser wants and Dispatch answers what the control "
                   "loop is doing about it; neither answers this.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "links": [],
    "panels": panels,
    "preload": False,
    # No forward horizon -- this dashboard has nothing to plan, only history to read back.
    "refresh": "5m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "health"],
    "templating": {"list": []},
    "time": {"from": "now-30d", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "Battery Health",
    "uid": "alphaess-battery-health",
    # BUMP THIS on every change below -- Grafana's file provisioner keeps the dashboard it
    # already stored unless the incoming version is higher, silently. TODO.md item 1 is two
    # live instances of exactly this being missed.
    # 1: first cut -- SoC/temp/power-limit stats, temperature history (copied from the main
    #    dashboard), power-limit history, and the fault/firmware/inverter-firmware/system-config
    #    raw tables. Cell voltage, SoH, remaining time, daily energy and lifetime cycles are
    #    deferred to the PR that adds their backend fields (TODO.md items 12-13) -- a dashboard
    #    panel naming a field dispatch/state.py never writes is exactly what
    #    test_every_field_filter_names_a_field_we_publish exists to catch.
    "version": 1,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
