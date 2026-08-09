"""Emit alphaess-energy-losses.json. This script is the source; the JSON is its output.

    python grafana/generate-energy-losses.py grafana/alphaess-energy-losses.json

Where the electricity goes that never reaches the house. Two losses, measured
by completely different means, which is why they are on one dashboard:

  conversion + standby  AlphaESS's own metered house load against the load
                        implied by power_readings, whose load_power_w is the
                        exact identity pv + grid + battery and therefore cannot
                        see a loss at all.
  battery internal      charge minus discharge, corrected for the energy still
                        sitting in the battery at the end of the window.

Both come from the `daily_energy` measurement that collector/efficiency.py
writes nightly. Every panel here reads stored numbers and sums them; nothing
recomputes. The integration behind those numbers is trapezoidal with
zero-crossing splitting over DST-aware local-day windows, and reimplementing
that in Flux is how a dashboard comes to quietly disagree with the module that
fed it.

Because the stored fields are per-day totals of an additive quantity, summing
them over the picked range is valid for any range -- there is no "start and end
SoC must match" precondition, which is what the SoC correction in
battery_loss_kwh buys.

Generated, not exported from the Grafana UI. tests/test_grafana_provisioning.py
re-runs this script and compares. Edit here, never in the JSON.
"""
import json, sys

DS = {"type": "influxdb", "uid": "${DS_ALPHAESS}"}

# Every panel starts from the same four lines. Kept as one string so a change to
# the bucket, measurement or version filter cannot be applied to eight queries
# and missed on the ninth. The model_version filter is not optional: without it
# a re-scored history is summed twice.
DAILY = '''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "daily_energy"
        and r.model_version == "${model_version}")'''

# aggregateWindow aligns its windows to the Unix epoch, i.e. UTC midnight, so a
# "1d" bucket without this covers 02:00 to 02:00 local in summer. daily_energy
# rows are already stamped at local midnight, so a mis-aligned window would put
# two of them in one bucket and none in the next.
LOCATION = '''import "timezone"
option location = timezone.location(name: "Europe/Amsterdam")

'''

# Time since the nightly job last ran -- NOT since the newest row's timestamp.
# daily_energy rows are stamped at the local midnight of the day they describe,
# so on a perfectly healthy system the newest row is 51 hours old just before
# the next run. A staleness check on that would need a >51h threshold and would
# take two and a half days to notice a dead job. computed_at_unix records when
# efficiency.py actually ran, so 30h catches a single missed night.
#
# Deliberately not on v.timeRangeStart: "is the job still running" is a fact
# about now, and reading it off the picker would make it go red merely because
# someone looked at last week.
JOB_AGE = '''import "array"

last = (from(bucket: "alphaess")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "daily_energy"
        and r._field == "computed_at_unix")
  |> max()
  |> findColumn(fn: (key) => true, column: "_value"))[0]

array.from(rows: [{
  _time: now(),
  _value: float(v: uint(v: now())) / 1000000000.0 - last
}])
  |> yield(name: "job age")
'''

# 30 hours. The same number lives in
# grafana/provisioning/alerting/alphaess-efficiency-staleness.yml, and a test
# pins them together so this box cannot read green while that alert fires.
JOB_AGE_RED_S = 108000
JOB_AGE_AMBER_S = 93600  # 26h: one late night, worth noticing, not worth paging


def target(query, ref="A"):
    return {"datasource": DS, "query": query, "refId": ref}


def sumOf(field, name):
    """Total of one field over the picked range.

    One field per query rather than a pivot: battery_loss_kwh and
    total_loss_kwh are written only when BATTERY_CAPACITY_KWH is configured,
    and a pivot referencing a column some rows lack fails the whole panel
    rather than the one series.
    """
    return DAILY + '''
  |> filter(fn: (r) => r._field == "%s")
  |> group()
  |> sum()
  |> yield(name: "%s")
''' % (field, name)


def ratioOf(numerator, denominator, name, scale="100.0"):
    """One sum divided by another, over the whole picked range.

    A ratio of two sums, not the mean of per-day ratios: a day that moved 2 kWh
    through the battery carries the same weight as one that moved 60 otherwise,
    and the answer stops meaning anything.
    """
    # `import` has to be the first statement in the file, not next to the call
    # that needs it: Flux rejects a mid-script import with "invalid statement".
    return '''import "array"

sums = ''' + DAILY + '''
  |> filter(fn: (r) => r._field == "%s" or r._field == "%s")
  |> group(columns: ["_field"])
  |> sum()

n = (sums |> filter(fn: (r) => r._field == "%s")
          |> findColumn(fn: (key) => true, column: "_value"))[0]
d = (sums |> filter(fn: (r) => r._field == "%s")
          |> findColumn(fn: (key) => true, column: "_value"))[0]

array.from(rows: [{
  _time: now(),
  _value: if d == 0.0 then 0.0 else n / d * %s
}])
  |> yield(name: "%s")
''' % (numerator, denominator, numerator, denominator, scale, name)


def daily(field, name):
    """One bar per local day. timeSrc "_start" because aggregateWindow
    otherwise stamps a window with its END, drawing Thursday's loss on Friday."""
    return LOCATION + DAILY + '''
  |> filter(fn: (r) => r._field == "%s")
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false, timeSrc: "_start")
  |> map(fn: (r) => ({ _time: r._time, "%s": r._value }))
  |> yield(name: "%s")
''' % (field, name, name)


def stat(id_, title, desc, query, unit, decimals, x, y, steps, w=6,
         color_mode="value"):
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
               fill=10, style="line", w=24, x=0, stacking="none", axis_label=""):
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
                    "axisLabel": axis_label,
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
                    "stacking": {"group": "A", "mode": stacking},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "green", "value": None}]},
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


def series_override(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def fixed(color):
    return {"id": "color", "value": {"fixedColor": color, "mode": "fixed"}}


BARS = {"id": "custom.drawStyle", "value": "bars"}

# Loss is a cost: more is worse. These are not health thresholds -- nothing here
# knows what "too much loss" is for a given house -- so the scales stay neutral
# except where a sign genuinely means something (a negative conversion loss is
# not a bonus, it is a data problem).
NEUTRAL = [{"color": "text", "value": None}]
LOSS_STEPS = [{"color": "orange", "value": None}, {"color": "text", "value": 0}]
EFF_STEPS = [{"color": "red", "value": None},
             {"color": "orange", "value": 90},
             {"color": "green", "value": 95}]

panels = []

# --- Row 1: the two losses and their total ---------------------------------------------
panels.append(stat(
    1, "Conversion + standby loss",
    "Total over the picked range of (load implied by power_readings) minus (load AlphaESS "
    "actually metered). This is the number the 30-second dataset structurally cannot "
    "produce on its own: load_power_w there is the exact identity pv + grid + battery, so "
    "its energy balance closes by construction and no loss can appear in it.\n\n"
    "It is a difference of two load figures, not a directly attributed physical loss -- it "
    "also contains anything AlphaESS meters on a different CT than the residual sees. "
    "Around 1-2.7 kWh a day on this system.",
    sumOf("conversion_loss_kwh", "conversion"), "kwatth", 2, 0, 0, LOSS_STEPS))

panels.append(stat(
    2, "Battery internal loss",
    "Charge minus discharge, minus the energy still in the battery at the end (delta SoC x "
    "capacity). The SoC correction is what makes this valid over any range instead of only "
    "over a window whose start and end SoC match.\n\n"
    "Written only when BATTERY_CAPACITY_KWH is configured. Single days can come out "
    "negative -- SoC is a BMS estimate, not a coulomb count, and it is nonlinear near the "
    "top and bottom of the range. That averages out over a week; do not read one day.",
    sumOf("battery_loss_kwh", "battery"), "kwatth", 2, 6, 0, LOSS_STEPS))

panels.append(stat(
    3, "Total AC-DC-AC loss",
    "The two added. They do not double-count: the battery term is measured at the battery "
    "(eCharge/eDischarge reproduce this repo's own integration of battery_power_w to within "
    "1.4% over 19 days), and the conversion term is measured entirely outside it, as the "
    "gap between two independent load figures.",
    sumOf("total_loss_kwh", "total"), "kwatth", 2, 12, 0, LOSS_STEPS))

panels.append(stat(
    4, "Job age",
    "Time since efficiency.py last ran, from computed_at_unix -- not since the newest row's "
    "timestamp, which is 51 hours old on a healthy system right before the next nightly "
    "run. Amber past 26h, red past 30h, matching the provisioned staleness alert.\n\n"
    "A job that silently stops otherwise looks exactly like a working dashboard: every "
    "panel keeps rendering the days it did write.",
    JOB_AGE, "s", 0, 18, 0,
    [{"color": "green", "value": None},
     {"color": "orange", "value": JOB_AGE_AMBER_S},
     {"color": "red", "value": JOB_AGE_RED_S}]))

# --- Row 2: the ratios, and the correction the battery figure rests on ------------------
panels.append(stat(
    5, "Round-trip efficiency",
    "Battery discharge divided by battery charge over the range, from AlphaESS's own daily "
    "counters. Independent of everything else here, which makes it the sanity check on the "
    "whole pipeline: measured directly from power_readings over a 20-day SoC-matched "
    "window it is 96.15%, so a number far from that means the pipeline is wrong, not the "
    "battery.\n\n"
    "Uncorrected for delta SoC, so a short range that ends fuller than it started reads "
    "low. Use a week or more.",
    ratioOf("discharge_kwh_api", "charge_kwh_api", "round-trip"), "percent", 2,
    0, 4, EFF_STEPS))

panels.append(stat(
    6, "Conversion loss as % of load",
    "The conversion + standby figure against the house load implied by power_readings, so "
    "it can be compared across ranges of different length.",
    ratioOf("conversion_loss_kwh", "derived_load_kwh", "share"), "percent", 2,
    6, 4, NEUTRAL))

panels.append(stat(
    7, "Delta SoC over range",
    "Net energy the battery gained or lost over the picked range. The battery-loss figure "
    "is charge - discharge - this, so showing it makes the correction visible instead of "
    "implied: when it is large, the loss number is leaning on BATTERY_CAPACITY_KWH being "
    "right. Verify that with `efficiency.py --system-facts`.",
    sumOf("delta_soc_kwh", "delta SoC"), "kwatth", 2, 12, 4, NEUTRAL))

panels.append(stat(
    8, "Grid-charged share of battery",
    "How much of everything the battery took in came from the grid rather than the roof. "
    "Nothing in power_readings can derive this -- eGridCharge is only reported by "
    "getOneDateEnergyBySn. 47% over the first 19 days measured.",
    ratioOf("grid_charge_kwh_api", "charge_kwh_api", "grid share"), "percent", 2,
    18, 4, NEUTRAL))

# --- Row 3: daily losses ---------------------------------------------------------------
panels.append(timeseries(
    9, "Loss per day",
    "The two losses stacked, one bar per local day. Conversion loss is the steadier of the "
    "two; the battery bar carries the SoC-estimate noise described on the stat above and "
    "can go negative on a single day.",
    [target(daily("conversion_loss_kwh", "conversion"), "A"),
     target(daily("battery_loss_kwh", "battery"), "B")],
    8, 8, "kwatth",
    [series_override("conversion", [fixed("orange"), BARS]),
     series_override("battery", [fixed("purple"), BARS])],
    fill=80, stacking="normal", axis_label="kWh"))

# --- Row 4: the two load figures the conversion loss is a difference of -----------------
panels.append(timeseries(
    10, "House load, metered vs derived",
    "The whole basis of the conversion figure. `derived` is pv + grid + battery from "
    "power_readings -- an identity, not a measurement. `metered` is what AlphaESS's "
    "5-minute series reports independently. The gap between them is the loss; if the two "
    "lines ever cross, something is wrong with the data rather than with the inverter.\n\n"
    "`derived at 5m` is the derived series resampled onto the metered instants. It should "
    "sit on top of `derived`: a visible gap there means the difference is a sampling "
    "artefact rather than a physical loss.",
    [target(daily("metered_load_kwh", "metered"), "A"),
     target(daily("derived_load_kwh", "derived"), "B"),
     target(daily("derived_load_kwh_at_5m", "derived at 5m"), "C")],
    16, 8, "kwatth",
    [series_override("metered", [fixed("green")]),
     series_override("derived", [fixed("blue")]),
     series_override("derived at 5m", [fixed("light-blue"),
                                       {"id": "custom.lineStyle",
                                        "value": {"dash": [10, 10], "fill": "dash"}},
                                       {"id": "custom.fillOpacity", "value": 0}])],
    fill=0, axis_label="kWh"))

# --- Row 5: where the battery's energy came from ----------------------------------------
panels.append(timeseries(
    11, "Battery charge: total vs from the grid",
    "eCharge against eGridCharge, per local day. The gap is what the roof put in. This is "
    "the field pricing.py's cost model wants and cannot currently get.",
    [target(daily("charge_kwh_api", "charged"), "A"),
     target(daily("grid_charge_kwh_api", "from grid"), "B"),
     target(daily("discharge_kwh_api", "discharged"), "C")],
    24, 8, "kwatth",
    [series_override("charged", [fixed("blue"), BARS]),
     series_override("from grid", [fixed("red"), BARS]),
     series_override("discharged", [fixed("green")])],
    fill=40, axis_label="kWh"))

# --- Row 6: the evidence behind every number above --------------------------------------
panels.append({
    "datasource": DS,
    "description":
        "Every stored field, per day, including the quality columns the gate ran on. "
        "`dropped` counts 5-minute records discarded as implausible -- AlphaESS "
        "intermittently returns a wildly wrong single `load` value (5832 W where the 30s "
        "series says 500 W), and 14 of them on 2026-08-01 were enough to take that day's "
        "conversion loss to -4.59 kWh before the filter existed. A day missing from this "
        "table failed the gate and was deliberately not stored; the reason is in the "
        "collector logs and in the Kuma notification.",
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "thresholds"},
            "custom": {
                "align": "auto", "cellOptions": {"type": "auto"},
                "filterable": False, "inspect": False,
            },
            "decimals": 2,
            "mappings": [],
            "thresholds": {"mode": "absolute",
                           "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [],
    },
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": 32},
    "id": 12,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": True, "displayName": "Time"}],
    },
    "pluginVersion": "11.6.0",
    "targets": [target(LOCATION + DAILY + '''
  |> filter(fn: (r) => r._field == "conversion_loss_kwh" or r._field == "battery_loss_kwh"
                    or r._field == "total_loss_kwh"
                    or r._field == "metered_load_kwh" or r._field == "derived_load_kwh"
                    or r._field == "charge_kwh_api" or r._field == "discharge_kwh_api"
                    or r._field == "grid_charge_kwh_api"
                    or r._field == "delta_soc_percent"
                    or r._field == "series_dropped" or r._field == "series_count"
                    or r._field == "series_coverage" or r._field == "readings_coverage"
                    or r._field == "soc_align_median_pp")
  |> aggregateWindow(every: 1d, fn: sum, createEmpty: false, timeSrc: "_start")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({
      _time: r._time,
      "conversion": r.conversion_loss_kwh,
      "battery": r.battery_loss_kwh,
      "total": r.total_loss_kwh,
      "metered load": r.metered_load_kwh,
      "derived load": r.derived_load_kwh,
      "charged": r.charge_kwh_api,
      "discharged": r.discharge_kwh_api,
      "from grid": r.grid_charge_kwh_api,
      "dSoC %": r.delta_soc_percent,
      "dropped": r.series_dropped,
      "records": r.series_count,
      "series cov": r.series_coverage,
      "readings cov": r.readings_coverage,
      "soc align pp": r.soc_align_median_pp
    }))
  |> yield(name: "daily")
''')],
    "title": "Day by day",
    "type": "table",
})

dashboard = {
    "__inputs": [{"name": "DS_ALPHAESS", "label": "alphaess", "description": "",
                  "type": "datasource", "pluginId": "influxdb",
                  "pluginName": "InfluxDB"}],
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
    "description": "Electricity lost converting AC to DC and back, from the daily_energy "
                   "measurement that efficiency.py writes nightly. Two independently "
                   "measured losses: the inverter's, and the battery's.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "links": [{
        "asDropdown": False,
        "icon": "external link",
        "includeVars": False,
        "keepTime": True,
        "tags": ["savings"],
        "targetBlank": False,
        "title": "Battery savings",
        "tooltip": "What the battery earned over the same days",
        "type": "dashboards",
        "url": "",
    }],
    "panels": panels,
    "preload": False,
    # Written once a night. A 5-minute refresh would re-run twelve queries
    # against numbers that cannot have changed.
    "refresh": "30m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "losses"],
    "templating": {"list": [{
        "current": {"text": "1", "value": "1"},
        "description": "daily_energy model_version to read. Bump in efficiency.py when the "
                       "stored schema or the loss definition changes; set the same value "
                       "here so old and new rows are not double-counted.",
        "hide": 0,
        "label": "Model version",
        "name": "model_version",
        "options": [{"selected": True, "text": "1", "value": "1"}],
        "query": "1",
        "skipUrlSync": False,
        "type": "custom",
    }]},
    # Whole days back and nothing forward: there is no row for a day that has
    # not ended. Two weeks because single days are noisy -- the battery term in
    # particular is only meaningful once the SoC estimate's error has averaged
    # out.
    "time": {"from": "now-14d/d", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "AlphaESS Energy Losses",
    "uid": "alphaess-energy-losses",
    # BUMP THIS on every change below. Grafana's file provisioner keeps the
    # dashboard it already stored unless the incoming version is higher -- it
    # reads the new file, compares, and does nothing, with no error and no log
    # line. The symptom is a fix that appears not to have worked.
    "version": 1,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
