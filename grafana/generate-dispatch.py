"""Generates grafana/alphaess-dispatch.json -- the Dispatch dashboard.

    python grafana/generate-dispatch.py grafana/alphaess-dispatch.json

EDIT THIS FILE, NEVER THE JSON. `tests/test_grafana_provisioning.py` re-runs every generator
and diffs the result, so a hand-edit to the output fails the suite by design.

WHY A SECOND DASHBOARD. `alphaess-battery-plan` is about the PLAN, and it carries a row of
dispatch stats in the middle of it because that was the cheapest place to put them while the
dispatcher was still being built. This one is about the COMMAND: is the loop alive, what is it
doing right now and why, did the last write land, what is it about to do, and what does the
register block actually say. Battery Plan is deliberately left alone -- the two overlap by a
few tiles and that is a smaller cost than moving panels somebody already knows where to find.

WRITTEN TO MATCH THE EXISTING DISPATCH ROW, not to improve on it. Section 7.3's staleness
guards were learned the hard way, twice, and the conventions they produced are copied here on
purpose:

  * ONE query constant with the window baked in. `-5m` is not a per-panel decision. A dead
    dispatcher does not clear `dispatch_state`; it leaves its last point sitting there
    forever, so a wide `last()` renders a command that expired an hour ago as the current
    state of the battery -- a confident wrong answer in the one place you go to check.
  * THE BASE THRESHOLD STEP IS A SEMANTIC CHOICE. Grafana colours `noValue` with it, so it is
    red only where absence means the loop is dead, and neutral everywhere absence is a normal
    resting state. A panel that cries wolf on a normal state is a panel you stop reading.
  * `noValue` is always a WORD, never blank.
  * CONDITIONAL FIELDS ARE NEVER GATED BY `last()` ALONE. `expires_at`, `slot_action`,
    `verified` and `soc_pct` stop being written the moment they mean nothing, so `last()`
    returns each at its own timestamp and the point a panel thinks it is reading never
    existed. The test is `exists` on the conditional field AND on an unconditional one.
  * THE COLOUR VOCABULARY IS THE DASHBOARD'S, not `review_page.py`'s. charge green,
    discharge orange, hold blue, self neutral. The review page uses a different palette;
    these tiles are read beside Battery Plan's and a reader comparing them must not have to
    translate between two schemes.

A RULE LEARNED WRITING THIS ONE, and it is not in the other generator because nothing there
needed it: **a `map` must carry `_time` through**. Dropping it -- returning only the computed
`_value` -- silently yields ZERO ROWS from a pivoted table, with no error and no warning. The
panel then renders `noValue`, which on the verdict tile below means it would have reported
`NO DISPATCHER` against a perfectly healthy loop. Verified against the live database on
2026-08-20: identical queries, one with `_time` in the map and one without, one row and none.
`keep(columns: ["_value"])` afterwards is still required for the string stats and is safe --
it is the map that must not drop it.
"""
import json, sys

DS = {"type": "influxdb", "uid": "${DS_ALPHAESS}"}

# Section 7.3 guard 1, and the reason it lives in a constant rather than in each panel.
DISPATCH_LAST = '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "%s")
  |> last()
'''

# For the STRING stats only, which reduce `/.*/` and therefore need exactly one column.
DISPATCH_LAST_VALUE = DISPATCH_LAST + '''  |> keep(columns: ["_value"])
'''

# Monitor #4's window, and `is-it-deciding.py`'s `STALE_PLAN_S`. Past this the dispatcher is
# serving a plan the translator should already have replaced, and `slots.MAX_PLAN_AGE` will
# shortly drop it to idle.
STALE_PLAN_S = 4 * 3600
# `is-it-deciding.py`/`review-dry-run.py`'s GAP_S: two missed ticks is not jitter.
GAP_S = 180

# --- The verdict -------------------------------------------------------------------------
#
# ONE TILE, EVALUATED IN PRIORITY ORDER, and the order is the whole design.
#
# GREEN MUST NOT MEAN "A COMMAND IS LIVE". `hold` and `self` are correct, common outcomes
# where little or nothing is written -- and #109 made `self` commoner still by releasing to
# self-consumption whenever there is surplus. A tile demanding an active command would be red
# most of the day, which is how a tile stops being read. That is the mistake panel 23 shipped
# and had to fix, and it is the same lesson as the base-step rule above.
#
# So red is reserved for the two states where the loop is TRYING AND FAILING, amber for an
# input that has gone bad, and blue for a deliberate configuration. Everything else is green.
#
# ANCHORED ON `live`, which is written on every tick by both point shapes. Every other field
# here is conditional, so `exists` against them is a same-instant test: `last()` per field
# plus `pivot` puts each field at its own timestamp, and the row carrying `live`'s newest
# timestamp is this tick. A conditional field appears on that row only if this tick wrote it.
#
# `exists` ON AN ABSENT COLUMN IS SAFE HERE and is not safe everywhere: pivot output has a
# dynamic record type, so a missing label evaluates to false at runtime. The same expression
# over `array.from`, whose record type is static, fails to compile with "record is missing
# label". Both verified against the live database; the difference matters if anyone ever
# tries to unit-test this string outside a pivot.
VERDICT = '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state")
  |> filter(fn: (r) => r._field == "live" or r._field == "read_error"
                    or r._field == "verified" or r._field == "plan_run")
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> filter(fn: (r) => exists r.live)
  |> map(fn: (r) => ({ _time: r._time, _value:
      if exists r.read_error then "BLIND"
      else if exists r.verified and r.verified == 0 then "NOT LANDING"
      else if not exists r.plan_run then "NO PLAN"
      else if float(v: int(v: now()) - int(v: time(v: r.plan_run))) / 1000000000.0 > %d.0
        then "STALE PLAN"
      else if r.live == 0 then "DRY RUN"
      else "DISPATCHING" }))
  |> keep(columns: ["_value"])
''' % STALE_PLAN_S

# Seconds since the last tick of any kind. DELIBERATELY NOT `findColumn`, which the plan
# dashboard's `Plan age` uses: `findColumn(...)[0]` on an empty result throws, and this is the
# one panel that has to keep working when there is nothing to read. No rows renders `noValue`,
# which is the honest answer and the one the verdict beside it is already giving.
#
# A TEN MINUTE WINDOW where everything else uses five, so the age keeps counting for a while
# after the verdict has gone to NO DISPATCHER. "Down, and for six minutes" is a more useful
# pair of readings than "down" beside a blank.
LAST_TICK = '''from(bucket: "alphaess")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "live")
  |> last()
  |> map(fn: (r) => ({ _time: r._time,
       _value: float(v: int(v: now()) - int(v: r._time)) / 1000000000.0 }))
  |> keep(columns: ["_value"])
'''

# The age of the plan THE DISPATCHER IS ACTUALLY SERVING, read back from its own points --
# not the newest run in the `planning` bucket, which is what Battery Plan's "Plan age" shows.
# The two differ exactly when it matters: a translator that has stopped leaves a fresh plan in
# the bucket and an old one in the dispatcher's hand, and only this reading can see that.
PLAN_AGE = '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "plan_run")
  |> last()
  |> map(fn: (r) => ({ _time: r._time,
       _value: float(v: int(v: now()) - int(v: time(v: r._value))) / 1000000000.0 }))
  |> keep(columns: ["_value"])
'''

# Panel 23's query, carried over unchanged -- see `generate-battery-plan.py` for the full
# reasoning. In short: gated on `expires_at` and `dispatch_active` being written at the SAME
# INSTANT, because a release writes the second without the first and counting a leftover
# `expires_at` down accuses a healthy dispatcher of having stopped after every release. The
# floor at zero is what keeps a loop that died mid-command pinned to the red step instead of
# sliding back to neutral.
EXPIRES_IN = '''from(bucket: "alphaess")
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
  |> map(fn: (r) => ({ r with _value: if r._value < 0.0 then 0.0 else r._value }))
  |> yield(name: "expires in")
'''

# --- The slots ---------------------------------------------------------------------------
#
# `dispatch_slots` is the translator's own output, published since #113. Before it, the only
# way to draw what the dispatcher was ABOUT to do was to reimplement `translator.classify()`
# in Flux -- which Battery Plan still does, and which cannot see #109's runtime release.
#
# NEWEST RUN ONLY, picked by PARSED INSTANT and never as a string: tags written before
# 2026-07-30 carry a `+02:00` offset, and "...17:26:14+02:00" sorts after "...16:00:00Z" while
# naming an instant half an hour earlier. Without the filter the table would mix runs, because
# slot boundaries move between them and an older run leaves slots at instants the newer one
# does not cover -- visible, plausible, and wrong.
SLOTS_BASE = '''newest = (from(bucket: "alphaess")
  |> range(start: -48h, stop: 72h)
  |> filter(fn: (r) => r._measurement == "dispatch_slots" and r._field == "action")
  |> keep(columns: ["plan_run"])
  |> group()
  |> distinct(column: "plan_run")
  |> map(fn: (r) => ({ tag: r._value, t: time(v: r._value) }))
  |> sort(columns: ["t"], desc: true)
  |> limit(n: 1)
  |> findColumn(fn: (key) => true, column: "tag"))[0]

nowS = float(v: int(v: now())) / 1000000000.0

// The whole of the newest run, elapsed slots included. Each panel narrows it differently.
run = from(bucket: "alphaess")
  |> range(start: -48h, stop: 72h)
  |> filter(fn: (r) => r._measurement == "dispatch_slots" and r.plan_run == newest)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> sort(columns: ["_time"])
'''

# THE TIMELINE KEEPS ELAPSED SLOTS. The table below does not, and the difference is the job
# each is doing: the table answers "what is next", so a slot that has already finished is
# noise in it. The timeline answers "what does the plan look like", and dropping the elapsed
# half would leave the left of the panel blank back to the start of the window -- which reads
# as "nothing was planned" rather than "that already happened".
#
# THE TERMINATOR ROW IS NOT DECORATION. A state timeline draws each value from its own point
# until the next one, so without a final row the last band runs to the right-hand edge of the
# panel forever -- rendering a horizon that ends at 22:00 as one that never ends. It carries
# the last slot's `end_s` and a value with no colour mapping, so it reads as the neutral gap
# it is. Taken from the whole run rather than the visible part, so it lands at the real end of
# the plan even when the window is showing less than all of it.
SLOT_BANDS = SLOTS_BASE + '''
bands = run
  |> map(fn: (r) => ({ _time: r._time, _value: r.action }))
tail = run
  |> tail(n: 1)
  |> map(fn: (r) => ({ _time: time(v: int(v: r.end_s) * 1000000000),
                       _value: "horizon end" }))

union(tables: [bands, tail])
  |> group()
  |> sort(columns: ["_time"])
  |> yield(name: "slots")
'''

# UPCOMING ONLY -- `end_s > nowS` keeps the slot that is running right now and drops the ones
# that have finished, so the top row is always what the battery is doing at this moment.
SLOT_TABLE = SLOTS_BASE + '''
run
  |> filter(fn: (r) => float(v: r.end_s) > nowS)
  |> map(fn: (r) => ({
      _time: r._time,
      action: r.action,
      minutes: r.duration_s / 60,
      power_w: r.power_w,
      target_soc: r.target_soc,
    }))
  |> yield(name: "slot table")
'''

# --- Commanded against actual ------------------------------------------------------------
#
# The panel that did not exist anywhere before this dashboard, and the one worth having. A
# command that the inverter accepts and does not honour is invisible on every other screen:
# `verified` proves the REGISTERS took the value, not that the battery moved.
#
# Each series leaves Flux in a column named after itself rather than in `_value`. Grafana
# names a field after its column but special-cases `_value` to the literal "Value", so two
# queries both yielding `_value` arrive as two fields called "Value", every `byName` override
# misses, and the panel silently falls back to its defaults. Seen on the NAS 2026-07-30.
COMMANDED = '''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "dispatch_state" and r._field == "setpoint_w")
  |> map(fn: (r) => ({ _time: r._time, Commanded: float(v: r._value) }))
  |> yield(name: "commanded")
'''

# NEGATED. The collector counts discharge positive; `setpoint_w` is already charging-positive
# (`registers.decode_power` flips it once, at the encoder). Two series on one axis disagreeing
# in sign would be worse than no panel at all -- panel 9 negates the same field for the same
# reason.
ACTUAL = '''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "battery_power_w")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ _time: r._time, Battery: -r._value }))
  |> yield(name: "actual")
'''

SOC_ACTUAL = '''from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "soc_percent")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({ _time: r._time, SoC: r._value }))
  |> yield(name: "soc")
'''

# --- Shortfall: commanded magnitude against the SAME-TICK battery reading ----------------
#
# MEASURED 2026-08-24: a commanded 4,700 W discharge under Mode 2 settled at 4,400-4,480 W for
# the full length of a 149-minute session, flat across 97%->61% SoC -- 6-7% under, and not
# anything `slots.clamp()` did. Charge over the same period met or slightly exceeded its own
# setpoint, so the shortfall is specific to sustained discharge: an inverter regulation
# asymmetry, invisible everywhere but an ad hoc Influx query until `actual_battery_w` existed.
#
# UNLIKE THE PANEL BELOW, this needs no window and no cross-series join: `setpoint_w` and
# `actual_battery_w` are two fields of the SAME POINT, both written from the same tick's
# readbacks (`scheduler.py` step 4 for the battery register, step 8 for the block), so `last()`
# plus `pivot` lands them on one row without the skew "Commanded against actual" has to allow
# for between this process's clock and the collector's.
#
# `%`, NOT `W`. The household changes the planner's `maxDischargeSpeed` from time to time, and
# a percentage keeps meaning the same thing across that change where a fixed watt threshold
# would not. Filtered to commands over 50 W so a hold (setpoint 0) never divides by zero.
SHORTFALL = '''mag = (v) => if v < 0.0 then -v else v

from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state"
                   and (r._field == "setpoint_w" or r._field == "actual_battery_w"
                     or r._field == "dispatch_active"))
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> filter(fn: (r) => exists r.setpoint_w and exists r.actual_battery_w
                   and exists r.dispatch_active and r.dispatch_active != 0
                   and mag(v: float(v: r.setpoint_w)) > 50.0)
  |> map(fn: (r) => ({ _time: r._time, _value:
       100.0 * (mag(v: float(v: r.setpoint_w)) - mag(v: float(v: r.actual_battery_w)))
       / mag(v: float(v: r.setpoint_w)) }))
  |> keep(columns: ["_value"])
'''

# --- The register decode table -----------------------------------------------------------
#
# Carried over from `generate-battery-plan.py` unchanged. The raw column is the point: it is
# what lets a decode be checked against the AlphaESS spec without leaving the page, and every
# encoding here was got wrong by somebody first.
#
# EVERY FIELD IN THE PIVOT IS WRITTEN UNCONDITIONALLY, and that is a constraint rather than a
# coincidence. `last()` returns each field at its own timestamp and `pivot` keys rows by that
# timestamp, so one conditional field in this list splits the table into two rows -- one
# populated, one blank -- for as long as the stale field stays inside the window. Five minutes
# of that after every release.
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


# --- Panel helpers, copied from generate-battery-plan.py ---------------------------------
#
# COPIED, NOT SHARED, and deliberately. The house rule is stated at
# `generate-battery-score.py:29` -- "the string is duplicated because these two scripts share
# no module, and tests pin every dashboard to the same query for exactly that reason". So
# `tests/test_dispatch_dashboard.py` pins `DISPATCH_LAST` identical across both generators
# instead, which catches drift at the only place it could do harm.


def target(query, ref="A"):
    return {"datasource": DS, "query": query, "refId": ref}


def stat(id_, title, desc, query, unit, decimals, x, w, steps, color_mode="value",
         y=0, mappings=None, no_value=None, string_value=False, justify_mode="auto",
         value_size=None):
    """A stat panel. Same signature as the plan generator's, so call sites read the same.

    `string_value` is not cosmetic. Grafana treats an empty `reduceOptions.fields` as AUTO,
    and auto means NUMERIC FIELDS ONLY -- a string field is discarded before any mapping is
    consulted and the panel renders `noValue` forever. `/.*/` fixes that and then requires the
    query to return exactly ONE column, which is what `DISPATCH_LAST_VALUE` is for.

    `value_size` pins the value's font size instead of Grafana's auto-fit. Auto-fit sizes for
    the WORST CASE it has ever rendered, so a tile whose value is sometimes a long sentence
    (see 'Why' below) shrinks to fit that sentence even on a tick where the text is short --
    unreadably small either way. A pinned size trades that off deliberately: long text wraps
    instead of shrinking further.
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
    options = {
        "colorMode": color_mode,
        "graphMode": "none",
        "justifyMode": justify_mode,
        "orientation": "auto",
        "percentChangeColorMode": "standard",
        "reduceOptions": {
            "calcs": ["lastNotNull"],
            "fields": "/.*/" if string_value else "",
            "values": False,
        },
        "showPercentChange": False,
        "textMode": "auto",
        "wideLayout": True,
    }
    if value_size is not None:
        options["text"] = {"valueSize": value_size}
    return {
        "datasource": DS,
        "description": desc,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "id": id_,
        "options": options,
        "pluginVersion": "11.6.0",
        "targets": [target(query)],
        "title": title,
        "type": "stat",
    }


def series_override(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


# The four actions `translator.classify` emits, in the DASHBOARD's colours rather than
# `review_page.py`'s. `self` is neutral on purpose: section 4.1 makes it the deliberate
# RELEASE of dispatch, so the battery running itself under it is the decision being honoured,
# not the absence of one.
ACTION_COLOURS = {
    "charge": {"color": "green", "index": 0},
    "discharge": {"color": "orange", "index": 1},
    "hold": {"color": "blue", "index": 2},
    "self": {"color": "text", "index": 3},
}

panels = []

# =========================================================================================
# Row A, y=0 -- is it working?
# =========================================================================================

panels.append(stat(
    1, "Dispatcher",
    "The one tile. DISPATCHING means the loop is alive, serving a fresh plan and writing. "
    "It stays green through 'hold' and 'self', which are decisions like any other and are "
    "most of a normal day - a tile that demanded an active command would be red by "
    "lunchtime and you would stop reading it. Red means the loop is trying and failing: "
    "BLIND is a Modbus read it could not do, NOT LANDING is a write the inverter took and "
    "did not honour. STALE PLAN means it is fine but flying on old information and will drop "
    "to idle. DRY RUN means it is deciding and writing nothing, on purpose. NO DISPATCHER "
    "means no point has arrived in five minutes: read 'Last tick' beside it.",
    VERDICT, "none", None, 0, 8,
    # RED IS THE BASE STEP HERE, and the only tile on this row where it is. Grafana colours
    # `noValue` with the base step, and on this panel absence means the loop is gone.
    [{"color": "red", "value": None}],
    no_value="NO DISPATCHER", string_value=True,
    mappings=[{"type": "value", "options": {
        "DISPATCHING": {"color": "green", "index": 0},
        "DRY RUN": {"color": "blue", "index": 1},
        "STALE PLAN": {"color": "orange", "index": 2},
        "NO PLAN": {"color": "orange", "index": 3},
        "NOT LANDING": {"color": "red", "index": 4},
        "BLIND": {"color": "red", "index": 5},
    }}]))

panels.append(stat(
    2, "Mode",
    "Whether the dispatcher is writing to the inverter at all. DISPATCH_LIVE=0 decides and "
    "publishes but touches no register, so every readback on this page stays flat whatever "
    "was decided - which is what the dry run is for, and is not a fault. Its own tile "
    "because the verdict beside it ranks a stale plan higher: in dry run a stale plan is "
    "still worth knowing about, and hiding one behind the other would lose a reading.",
    DISPATCH_LAST % "live", "none", None, 8, 4,
    [{"color": "text", "value": None}],
    y=0, no_value="unknown",
    mappings=[{"type": "value", "options": {
        "1": {"text": "live", "color": "green", "index": 0},
        "0": {"text": "dry run", "color": "blue", "index": 1},
    }}]))

panels.append(stat(
    3, "Last tick",
    "Seconds since the loop last published anything. It should sit under 60. Past 180 - two "
    "missed ticks, the same threshold review-dry-run.py and Kuma monitor #5 use - it is not "
    "jitter. This reads over ten minutes where the rest of the row reads five, so it keeps "
    "counting after the verdict has gone to NO DISPATCHER: 'down, and for six minutes' is "
    "more use than 'down' beside a blank.",
    LAST_TICK, "s", 0, 12, 4,
    [{"color": "green", "value": None}, {"color": "red", "value": GAP_S}],
    y=0, no_value="silent"))

panels.append(stat(
    4, "Plan age",
    "How old the plan the DISPATCHER IS HOLDING is - read back from its own points, not "
    "from the newest run in the planning bucket. The two differ exactly when it matters: a "
    "translator that has stopped leaves a fresh plan in the bucket and an old one in the "
    "dispatcher's hand, and only this reading can see that. Past four hours slots.py calls "
    "it stale and the loop goes idle, which is Kuma monitor #4.",
    PLAN_AGE, "s", 0, 16, 4,
    [{"color": "green", "value": None}, {"color": "red", "value": STALE_PLAN_S}],
    y=0, no_value="no plan"))

panels.append(stat(
    5, "Verified",
    "Did the last write land? The dispatcher reads the block back after every command and "
    "compares it with what it sent. 'nothing to verify' is the normal resting state, not a "
    "failure: a release or an idle tick commands nothing, so a readback proves nothing - "
    "which is also why Kuma monitor #6 stays up through it. NOT LANDING is the failure this "
    "whole design fears most, the one where every log line says commanded and the battery "
    "does nothing.",
    DISPATCH_LAST % "verified", "none", None, 20, 4,
    # NEUTRAL BASE. This field is conditional - absent on every release and every idle tick -
    # and a red base step would paint most of a normal day as a failed write.
    [{"color": "text", "value": None}],
    y=0, no_value="nothing to verify",
    mappings=[{"type": "value", "options": {
        "1": {"text": "landed", "color": "green", "index": 0},
        "0": {"text": "NOT LANDING", "color": "red", "index": 1},
    }}]))

# =========================================================================================
# Row B, y=4 -- what is it doing, in words?
# =========================================================================================

panels.append(stat(
    6, "Decision",
    "What the PLAN asked for in the slot covering right now, from slot_action. 'no slot' is "
    "normal - it means the plan has nothing scheduled for this moment, not that the "
    "dispatcher is down. In dry run this is the only tile on the page that moves.",
    DISPATCH_LAST_VALUE % "slot_action", "none", None, 0, 5,
    [{"color": "text", "value": None}],
    y=4, no_value="no slot", string_value=True,
    mappings=[{"type": "value", "options": ACTION_COLOURS}]))

panels.append(stat(
    7, "Doing",
    "What the loop actually did with that decision. 'command' wrote the block; 'release' "
    "handed the battery back to self-consumption; 'idle' wrote nothing at all, which is the "
    "fail-safe. READ IT BESIDE 'Decision', because since #109 the two legitimately disagree: "
    "a hold whose charge target is already met releases to soak up surplus solar rather than "
    "freezing the battery while the sun spills. Decision 'hold' with Doing 'release' is that "
    "working, and 'Why' says so.",
    DISPATCH_LAST_VALUE % "decision_kind", "none", None, 5, 4,
    [{"color": "text", "value": None}],
    y=4, no_value="unknown", string_value=True,
    mappings=[{"type": "value", "options": {
        "command": {"color": "green", "index": 0},
        "release": {"color": "text", "index": 1},
        # Not red: idle IS the fail-safe and is the correct response to a stale plan. The
        # verdict tile above is what says whether the cause needs attention.
        "idle": {"color": "orange", "index": 2},
    }}]))

panels.append(stat(
    8, "Why",
    "The dispatcher's own sentence for this tick, in the words it logs. 'hold at 0 W', "
    "'plan stale (4.2 h)', 'charge downgraded to hold: target 40% not above live SoC 47%'. "
    "This is the tile that turns a red verdict into something you can act on, and until the "
    "field was published it existed only in the container log.",
    DISPATCH_LAST_VALUE % "reason", "none", None, 9, 7,
    [{"color": "text", "value": None}],
    y=4, no_value="no reason given", string_value=True,
    justify_mode="left", value_size=16))

panels.append(stat(
    9, "SoC now",
    "The battery level the dispatcher read before deciding - its number, not the "
    "collector's, and the one the direction rule was applied to. A discharge refused "
    "because the target is not below this is a decision you can only follow with this "
    "reading in front of you.",
    DISPATCH_LAST % "soc_pct", "percent", 1, 16, 4,
    [{"color": "text", "value": None}],
    y=4, no_value="unreadable"))

panels.append(stat(
    10, "Command expires in",
    "Time left on the dead man's switch. The loop rewrites it every 60 s, so this should sit "
    "near five minutes and never fall far; draining toward zero means the loop has stopped "
    "refreshing and the inverter is about to revert to self-consumption on its own. A grey "
    "'no command' means nothing is dispatching, which is a normal resting state and the "
    "whole of a dry-run day, not a fault.",
    EXPIRES_IN, "s", 0, 20, 4,
    [{"color": "text", "value": None}, {"color": "red", "value": 0},
     {"color": "green", "value": 60}],
    y=4, no_value="no command"))

# =========================================================================================
# Row B2, y=8 -- not just landed, but at the commanded MAGNITUDE
# =========================================================================================
#
# `Verified` on Row A proves the REGISTERS took the value; `actual_battery_w` -- read in the
# same Modbus round-trip as the surplus check, not from the collector -- proves the BATTERY
# did too. MEASURED 2026-08-24: a commanded 4,700 W discharge settled at ~4,400 W for a full
# 149-minute session with `verified` green the whole time. That gap is what these three tiles
# exist to catch, and none of Row A can see it.

# GATED ON A LIVE COMMAND, byte-identical to Battery Plan's panel 21 -- 0x0881 is a register
# and `scheduler.release()` writes only REG_START=0, so it stands after the command that set
# it is gone. `silent` was already the right word for the absence; without the gate it was
# unreachable, because a register that keeps its last value is never absent.
panels.append(stat(
    15, "Commanded now",
    "The setpoint the dispatcher just wrote, charging-positive. Only shown while a command "
    "is actually live: 0x0881 keeps its last value through a release, so 'silent' means "
    "nothing is commanded, not 0 W. Repeats 'Commanded against actual' below as a single "
    "number, for the phone-in-the-kitchen glance that chart is too wide for.",
    '''from(bucket: "alphaess")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state"
                   and (r._field == "setpoint_w" or r._field == "dispatch_active"))
  |> last()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> filter(fn: (r) => exists r.setpoint_w and exists r.dispatch_active
                   and r.dispatch_active != 0)
  |> map(fn: (r) => ({ _time: r._time, _value: float(v: r.setpoint_w) }))
  |> yield(name: "commanded power")
''', "watt", 2, 0, 8,
    [{"color": "text", "value": None}],
    y=8, no_value="silent"))

panels.append(stat(
    16, "Actual now",
    "REG_BATTERY_POWER, read in the same Modbus round-trip as the surplus check and flipped "
    "to the same charging-positive convention as 'Commanded now' beside it -- not the "
    "collector's reading, so the two land on one point and never need a cross-series join to "
    "agree.",
    DISPATCH_LAST % "actual_battery_w", "watt", 2, 8, 8,
    [{"color": "text", "value": None}],
    y=8, no_value="unreadable"))

panels.append(stat(
    17, "Shortfall",
    "How far short of the commanded MAGNITUDE the battery is actually running, as a "
    "percentage so it keeps meaning the same thing if the planner's tuned discharge speed "
    "changes. MEASURED 2026-08-24: sustained discharge at 4,700 W settled 6-7% under for a "
    "full 149-minute session while every other tile on this page stayed green. Negative "
    "means the battery is exceeding its setpoint, which charge sessions do routinely and is "
    "not a fault. 'no command' is the resting state: hold and self both write a setpoint "
    "under 50 W, which this ignores rather than divide by; so does a release, which leaves "
    "the last setpoint standing in the register and is gated on separately.",
    SHORTFALL, "percent", 1, 16, 8,
    # Matches `slots.SHORTFALL_PCT` (5%) and twice it for red -- keep the two in sync by hand,
    # the same trade `generate-battery-score.py:29` makes for every constant shared this way.
    [{"color": "green", "value": None}, {"color": "orange", "value": 5.0},
     {"color": "red", "value": 10.0}],
    y=8, no_value="no command"))

# =========================================================================================
# Row C, y=12 -- did it land?
# =========================================================================================
#
# BOTH WATT SERIES SHARE THE LEFT AXIS, so they must agree on unit AND axisLabel --
# `tests/test_grafana_provisioning.py:135` fails the build otherwise, and two power series on
# one axis under two different labels is exactly the confusion it exists to stop.
panels.append({
    "datasource": DS,
    "description": "The command against what the battery actually did. `verified` on the row "
                   "above proves the REGISTERS took the value; this is the only thing on any "
                   "dashboard that shows whether the battery moved. A commanded charge the "
                   "inverter accepts and quietly ignores looks identical to a healthy one "
                   "everywhere else. Commanded is drawn as a step because it is an "
                   "instruction, not a measurement: it holds its value until the next write. "
                   "Both lines are charging-positive - the collector reports discharge "
                   "positive and is negated here, so a gap between the two is a real "
                   "disagreement rather than a sign convention.",
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "palette-classic"},
            "custom": {
                "axisBorderShow": False,
                "axisCenteredZero": True,
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
            "unit": "watt",
        },
        "overrides": [
            series_override("Commanded", [
                {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
                {"id": "custom.lineInterpolation", "value": "stepAfter"},
                {"id": "custom.lineWidth", "value": 2},
                {"id": "unit", "value": "watt"},
                {"id": "custom.axisPlacement", "value": "left"},
                {"id": "custom.axisLabel", "value": "W, charging positive"}]),
            series_override("Battery", [
                {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}},
                {"id": "custom.fillOpacity", "value": 10},
                {"id": "unit", "value": "watt"},
                {"id": "custom.axisPlacement", "value": "left"},
                {"id": "custom.axisLabel", "value": "W, charging positive"}]),
            series_override("SoC", [
                {"id": "color", "value": {"fixedColor": "text", "mode": "fixed"}},
                {"id": "custom.lineStyle",
                 "value": {"dash": [10, 10], "fill": "dash"}},
                {"id": "unit", "value": "percent"},
                {"id": "max", "value": 100},
                {"id": "min", "value": 0},
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "custom.axisLabel", "value": "SoC %"}]),
        ],
    },
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 12},
    "id": 11,
    # ITS OWN WINDOW, backward only. The dashboard runs to now+36h so the slot panels can
    # draw the whole horizon, and this panel is a record of what already happened -- given
    # the dashboard's range it would spend two thirds of its width on empty future.
    "timeFrom": "12h",
    "options": {
        "legend": {"calcs": [], "displayMode": "list", "placement": "bottom",
                   "showLegend": True},
        "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"},
    },
    "pluginVersion": "11.6.0",
    "targets": [target(COMMANDED, "A"), target(ACTUAL, "B"), target(SOC_ACTUAL, "C")],
    "title": "Commanded against actual",
    "type": "timeseries",
})

# =========================================================================================
# Row D, y=20 and y=24 -- what is coming
# =========================================================================================
panels.append({
    "datasource": DS,
    "description": "What the dispatcher will do next, as blocks - read from the translator's "
                   "own slots rather than worked out again from the plan. Contiguous "
                   "intervals with the same action, power and target are already merged into "
                   "one slot, so a run of fifteen-minute discharges reads as one evening "
                   "band. The neutral 'horizon end' block is where the plan stops; without it "
                   "the last band would run to the edge of the panel and a horizon that ends "
                   "at 22:00 would look like one that never ends.",
    "fieldConfig": {
        "defaults": {
            "color": {"mode": "thresholds"},
            "custom": {
                "fillOpacity": 70,
                "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                "insertNulls": False,
                "lineWidth": 0,
                "spanNulls": False,
            },
            "mappings": [{"type": "value", "options": dict(
                ACTION_COLOURS,
                **{"horizon end": {"color": "text", "index": 4}})}],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
            "noValue": "no slots published - has the translator run since #113?",
        },
        "overrides": [],
    },
    "gridPos": {"h": 4, "w": 24, "x": 0, "y": 20},
    "id": 12,
    "options": {
        "alignValue": "left",
        "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
        "mergeValues": True,
        "rowHeight": 0.9,
        "showValue": "auto",
        "tooltip": {"hideZeros": False, "mode": "single", "sort": "none"},
    },
    "pluginVersion": "11.6.0",
    "targets": [target(SLOT_BANDS)],
    "title": "What the dispatcher will do next",
    "type": "state-timeline",
})

# Explicit column widths, for the same reason #92 gave the plan tables theirs: without them
# Grafana stretches four columns across 24 units and the table stops being readable on a phone.
panels.append({
    "datasource": DS,
    "description": "The same slots as rows. Power and target SoC are blank on hold and self "
                   "by design - those actions carry no setpoint, and a zero would read as "
                   "'charge at 0 W to 0 %', which is a command this dispatcher can actually "
                   "issue. Only slots that have not finished yet are listed, so the top row "
                   "is what is running now.",
    "fieldConfig": {
        "defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            "mappings": [],
            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
        },
        "overrides": [
            {"matcher": {"id": "byName", "options": "_time"},
             "properties": [{"id": "displayName", "value": "From"},
                            {"id": "custom.width", "value": 150},
                            {"id": "unit", "value": "time:ddd HH:mm"}]},
            {"matcher": {"id": "byName", "options": "action"},
             "properties": [{"id": "displayName", "value": "Action"},
                            {"id": "custom.width", "value": 110},
                            {"id": "mappings",
                             "value": [{"type": "value", "options": ACTION_COLOURS}]},
                            {"id": "custom.cellOptions",
                             "value": {"type": "color-text"}}]},
            {"matcher": {"id": "byName", "options": "minutes"},
             "properties": [{"id": "displayName", "value": "For"},
                            {"id": "custom.width", "value": 90},
                            {"id": "unit", "value": "m"},
                            {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "power_w"},
             "properties": [{"id": "displayName", "value": "Power"},
                            {"id": "custom.width", "value": 110},
                            {"id": "unit", "value": "watt"},
                            {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "target_soc"},
             "properties": [{"id": "displayName", "value": "Target SoC"},
                            {"id": "custom.width", "value": 120},
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 1}]},
        ],
    },
    "gridPos": {"h": 10, "w": 24, "x": 0, "y": 24},
    "id": 13,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
        "sortBy": [{"desc": False, "displayName": "From"}],
    },
    "pluginVersion": "11.6.0",
    # Flux does not promise an output column order, so it is pinned here rather than left to
    # the order of the fields in the `map`. Same reason as the plan dashboard's tables.
    "transformations": [{
        "id": "organize",
        "options": {
            "excludeByName": {},
            "includeByName": {},
            "renameByName": {},
            "indexByName": {"_time": 0, "action": 1, "minutes": 2, "power_w": 3,
                            "target_soc": 4},
        },
    }],
    "targets": [target(SLOT_TABLE)],
    "title": "Slots ahead",
    "type": "table",
})

# =========================================================================================
# Row E, y=34 -- the registers themselves
# =========================================================================================
panels.append({
    "datasource": DS,
    "description": "The dispatch block as it reads right now, decoded. The raw column is kept "
                   "deliberately: it is what lets a decode be checked against the AlphaESS "
                   "register spec without leaving this page, and every encoding here was got "
                   "wrong by somebody first. 0x0881 and 0x0887 are 32-bit and are shown "
                   "recombined from their two words. 0x0883 is reactive power and is never "
                   "written. Empty means no point in five minutes - see the verdict at the "
                   "top.",
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
            {"matcher": {"id": "byName", "options": "name"},
             "properties": [{"id": "displayName", "value": "Name"},
                            {"id": "custom.width", "value": 160}]},
            {"matcher": {"id": "byName", "options": "raw"},
             "properties": [{"id": "displayName", "value": "Raw"},
                            {"id": "custom.width", "value": 120},
                            {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "means"},
             "properties": [{"id": "displayName", "value": "Means"}]},
        ],
    },
    # h=8, not 6. Five registers plus a header does not fit in six rows of grid, and the two
    # that fall off the bottom are 0x0886 and 0x0887 -- the SoC target and the dead man's
    # switch, which are the two worth checking when a command looks wrong.
    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 34},
    "id": 14,
    "options": {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
    },
    "pluginVersion": "11.6.0",
    # FLUX DOES NOT PROMISE AN OUTPUT COLUMN ORDER, and `union` of five `map`s does not
    # produce one either -- the table rendered Means first, so the column you read last came
    # first and the register you were looking for came second. The other generated tables all
    # pin this; the decode table was the one that did not.
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
        {"type": "panel", "id": "state-timeline", "name": "State timeline", "version": ""},
        {"type": "panel", "id": "table", "name": "Table", "version": ""},
        {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
    ],
    "annotations": {"list": []},
    "description": "What the dispatcher is telling the battery to do, whether it landed, and "
                   "what it will do next. Battery Plan answers what the optimiser wants; this "
                   "answers what the control loop is actually doing about it.",
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "links": [],
    "panels": panels,
    "preload": False,
    # Faster than the plan dashboard's 5 m, and for a different job: this page is read to find
    # out whether something is wrong right now, and a tile that is up to five minutes stale
    # cannot answer that on a loop that ticks every sixty seconds.
    "refresh": "1m",
    "schemaVersion": 42,
    "tags": ["alphaess", "battery", "dispatch"],
    # No variables. The horizon the slot panels show is the time picker's, which is the
    # control every other dashboard here already uses and the only one that moves the axis
    # as well as the query.
    "templating": {"list": []},
    # TWELVE BACK, THIRTY-SIX FORWARD, and the forward half is the load-bearing part: every
    # Grafana panel renders only inside the dashboard window, so a slot query returning the
    # full horizon still draws nothing past the right-hand edge. The first cut of this
    # dashboard stopped at now+6h and the timeline showed six hours under a title promising
    # thirty-six -- the query was right and the axis was lying about it.
    #
    # "Commanded against actual" is the one panel this is wrong for, since it is a record of
    # what already happened, so it carries its own backward-only window instead.
    "time": {"from": "now-12h", "to": "now+36h"},
    "timepicker": {},
    "timezone": "browser",
    "title": "Dispatch",
    "uid": "alphaess-dispatch",
    # BUMP THIS on every change below. Grafana's file provisioner keeps the dashboard it
    # already stored unless the incoming version is higher - it reads the new file, compares,
    # and does nothing, with no error and no log line. Restarting or recreating the container
    # does not help. The symptom is a fix that appears not to have worked, which sends you
    # back to re-debug a query that was already correct. TODO.md item 1 is two live instances
    # of exactly this on the plan dashboard.
    # 1: first cut - verdict row, command row, commanded-against-actual, slots ahead, decode.
    # 2: window runs to now+36h so the slot panels can draw the whole horizon; the horizon
    #    variable is gone, the chart keeps its own backward window, and the decode table gets
    #    a pinned column order and room for all five registers.
    # 3: Row B2 -- Commanded now / Actual now / Shortfall, reading the new `actual_battery_w`
    #    field. Rows C, D and E all move down 4 to make room.
    # 4: 'Actual now' shows 2 decimals instead of 0, to stop it disagreeing with the plan
    #    dashboard's 'Battery Power' tile by nothing but rounding (4.8 kW showing as "5").
    # 5: 'Why' is left-justified with a pinned value size instead of auto-fit, which shrank
    #    its longer reason sentences to near-unreadable size.
    # 6: 'Commanded now' and 'Shortfall' are gated on a live command. 0x0881 keeps its last
    #    value through a release, so the tile reported a setpoint for a battery nothing was
    #    commanding -- and 'silent' was unreachable, because a stale register is never absent.
    #    Shortfall had the worse half: it DIVIDES by that setpoint, so a release after a
    #    -2,500 W discharge read 100% short and went red while the battery sat correctly at
    #    0 W. Its 50 W floor never caught this -- it excludes hold and self, not a release.
    "version": 6,
    "weekStart": "",
}

out = sys.argv[1]
with open(out, "w") as fh:
    json.dump(dashboard, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("wrote %s (%d panels)" % (out, len(panels)))
