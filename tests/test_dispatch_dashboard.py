"""The dashboard's contract with `dispatch_state`. DESIGN-dispatch.md section 7.

`dispatch/state.py` writes the fields; `grafana/generate-battery-plan.py` reads them. Nothing
connects the two but a string, and a mismatch is invisible in the worst way: Flux does not
fail on an unknown column when the stream is empty, and the stream is empty exactly when the
dispatcher is down -- so the panel that was supposed to shout NO DISPATCHER renders a blank
cell and a typo waits until the first live run to be found.

Running the queries against a live InfluxDB does not catch it either, for the same reason:
with no `dispatch_state` points the maps are never evaluated. So the check has to be static.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import registers as R
from state import build_fields

DASHBOARDS = Path(__file__).parent.parent / "grafana"
MEASUREMENT = "dispatch_state"

# Columns Flux itself supplies, which are legitimately referenced but are not our fields.
FLUX_COLUMNS = {"_time", "_value", "_field", "_measurement", "_start", "_stop", "sys_sn"}


def published_field_values() -> dict:
    """A fully populated `dispatch_state` point, values kept.

    Separate from `published_fields` because the value TYPES matter too: Grafana treats
    string and numeric fields differently, and a panel reading one as the other fails
    silently -- see `test_the_state_stat_can_reach_its_mappings`.
    """
    words = [1, *R.encode_power(-4500), 0, 0, 2, 50, *R.encode_int32(300)]
    return build_fields(
        R.decode_block(words), words, dt.datetime.now(dt.UTC),
        decision_kind="command",
        slot={"start": "2026-08-15T18:15:00Z", "action": "discharge"},
        plan_run="2026-08-15T15:00:00Z")


def published_fields() -> set[str]:
    """Every field name `state.build_fields` can emit, for a fully populated command."""
    return set(published_field_values())


def conditional_fields() -> set[str]:
    """Fields `build_fields` writes for a live command but NOT for a released one.

    Derived rather than listed, so adding another conditional field to `state.py` puts it
    under these guards automatically instead of waiting to be noticed on the dashboard.
    """
    words = [0, *R.encode_power(0), 0, 0, 3, 0, *R.encode_int32(0)]
    released = set(build_fields(
        R.decode_block(words), words, dt.datetime.now(dt.UTC), decision_kind="release"))
    return published_fields() - released


def dispatch_queries() -> list[tuple[str, str, str]]:
    """(dashboard, panel title, query) for every query touching `dispatch_state`."""
    out = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        panels = json.loads(path.read_text()).get("panels", [])
        for panel in panels:
            for t in panel.get("targets", []):
                q = t.get("query", "")
                if MEASUREMENT in q:
                    out.append((path.name, panel.get("title", "?"), q))
    return out


def _stat_panels() -> list[tuple[str, str, dict]]:
    """(dashboard, panel title, panel) for every stat and gauge, across all dashboards.

    Both share the `reduceOptions` field picker, which is where this class's last two guards
    live, and neither bug was specific to one dashboard.
    """
    out = []
    for path in sorted(DASHBOARDS.glob("*.json")):
        for panel in json.loads(path.read_text()).get("panels", []):
            if panel.get("type") in ("stat", "gauge"):
                out.append((path.name, panel.get("title", "?"), panel))
    return out


class TestFieldContract:
    def test_at_least_one_panel_reads_dispatch_state(self):
        """Guards the guard: a rename that made this file test nothing would be silent."""
        assert dispatch_queries()

    def test_every_referenced_column_is_a_field_we_publish(self):
        """The check this file exists for.

        `r.raw_880` instead of `r.raw_0880` is accepted by Flux, by the JSON, and by a live
        query against an empty measurement. It fails only once real data arrives -- which is
        the first live dispatch run, the least convenient moment to discover it.
        """
        known = published_fields() | FLUX_COLUMNS
        for dashboard, title, query in dispatch_queries():
            for column in set(re.findall(r"\br\.([A-Za-z_][A-Za-z0-9_]*)", query)):
                assert column in known, (
                    f"{dashboard} / {title}: query reads r.{column}, which "
                    f"dispatch/state.py never writes")

    def test_every_field_filter_names_a_field_we_publish(self):
        """`_field == "..."` is the other way a name enters a query."""
        known = published_fields()
        for dashboard, title, query in dispatch_queries():
            if MEASUREMENT not in query:
                continue
            for name in set(re.findall(r'_field\s*==\s*"([^"]+)"', query)):
                assert name in known, (
                    f"{dashboard} / {title}: filters on _field == {name!r}, which "
                    f"dispatch/state.py never writes")

    def test_the_raw_block_is_referenced_by_its_hex_address(self):
        """The decode table's whole purpose is checking a decode against the spec, which is
        indexed in hex. `raw_2181` would be the same register and the wrong label."""
        for _dash, _title, query in dispatch_queries():
            assert not re.search(r"\braw_\d{4}\b(?<!raw_08\d\d)", query)


class TestConditionalFields:
    """The root cause behind three separate panel bugs, and the reason it is worth a class.

    `state.py` writes `expires_at`, `slot_start`, `slot_action` and `plan_run` only when they
    mean something. Every panel here was written as if all fields arrive on every tick. They
    do not, and the gap is invisible in test and in a fresh deployment -- it opens the first
    time the dispatcher releases a command in production, which is a normal thing that
    happens several times a day.
    """

    def test_there_are_conditional_fields_to_guard(self):
        """Guards the guard: if `state.py` ever writes everything unconditionally these
        tests would pass by being vacuous."""
        assert conditional_fields() == {"expires_at", "slot_start", "slot_action", "plan_run"}

    def test_the_decode_table_reads_only_unconditionally_written_fields(self):
        """`last()` returns each field's newest point WITH ITS OWN TIMESTAMP, and `pivot`
        keys rows by that timestamp. Mixing a stale conditional field with fields still being
        written yields two rows at two instants, and the table's union renders every register
        twice -- one populated copy, one blank -- until the stale field ages out of the
        window. Five minutes of that after every single release."""
        table = next(q for dash, title, q in dispatch_queries() if "pivot" in q and "union" in q)
        for field in conditional_fields():
            assert f'"{field}"' not in table, (
                f"the decode table pulls {field!r} into its pivot; that field stops being "
                f"written on release and will split the table into two rows")

    def test_the_expiry_countdown_cannot_run_off_a_stale_expires_at(self):
        """`expires_at` outlives the command that set it by up to the query window, so a
        `last()` on it alone counts down through a normal release -- turning the panel red
        and reporting a healthy dispatcher as stopped, every time it releases.

        The gate has to be a same-instant test rather than a plain `dispatch_active` check,
        because the case the panel EXISTS for -- a loop that dies mid-command -- leaves both
        fields stale together and must still show the drain."""
        q = next(q for dash, title, q in dispatch_queries() if "expires_at" in q)
        assert "exists" in q, "the countdown does not check whether expires_at is still live"
        assert "dispatch_active" in q, "the countdown is not gated on a command being live"


class TestStalenessGuards:
    """Section 7.3. A dead dispatcher does not clear `dispatch_state` -- it leaves the last
    point sitting there forever. A panel querying a wide range with `last()` renders a
    command that expired fifty minutes ago as the current state of the battery: a confident
    wrong answer in the one place you go to check."""

    def test_every_last_value_query_uses_a_short_window(self):
        for dashboard, title, query in dispatch_queries():
            if "|> last()" not in query:
                continue
            windows = re.findall(r"range\(start:\s*(-?\w+)", query)
            assert windows, f"{dashboard} / {title}: last() with no range"
            for w in windows:
                assert w in ("-5m", "-10m"), (
                    f"{dashboard} / {title}: last() over {w} would render a stale command "
                    f"as current -- section 7.3 requires a window of a few loop iterations")

    def test_the_state_stat_declares_a_loud_no_value(self):
        """`Released - following house` and `NO DISPATCHER` are the SAME register contents:
        start=0. Only freshness separates them, so the absence has to be loud."""
        plan = json.loads((DASHBOARDS / "alphaess-battery-plan.json").read_text())
        panel = next(p for p in plan["panels"] if p.get("title") == "Dispatch state")
        defaults = panel["fieldConfig"]["defaults"]
        assert defaults["noValue"] == "NO DISPATCHER"
        # The base threshold step is what colours a no-data reading, so it must be the
        # alarming one; the mappings below it colour the states actually commanded.
        assert defaults["thresholds"]["steps"][0]["color"] == "red"

    def test_the_state_stat_maps_every_action_the_dispatcher_can_emit(self):
        """A state with no mapping renders in the base colour -- red -- and would read as a
        failure while the dispatcher was working perfectly."""
        from state import describe_action

        plan = json.loads((DASHBOARDS / "alphaess-battery-plan.json").read_text())
        panel = next(p for p in plan["panels"] if p.get("title") == "Dispatch state")
        mapped = set(panel["fieldConfig"]["defaults"]["mappings"][0]["options"])

        emitted = set()
        for mode in (1, 2, 3):
            for power in (-4500, 0, 4000):
                words = [1, *R.encode_power(power), 0, 0, mode, 50, *R.encode_int32(300)]
                emitted.add(describe_action(R.decode_block(words)))
        for kind in ("release", "idle", ""):
            words = [0, *R.encode_power(0), 0, 0, 3, 50, *R.encode_int32(300)]
            emitted.add(describe_action(R.decode_block(words), kind))

        assert emitted <= mapped, f"unmapped dispatch states: {sorted(emitted - mapped)}"

    def test_the_state_stat_can_reach_its_mappings(self):
        """Mapping every action is not enough if the panel never sees the value.

        Grafana's field picker treats an empty `reduceOptions.fields` as AUTO, and auto means
        NUMERIC FIELDS ONLY. `action` is a string, so it was dropped before any mapping was
        consulted, the panel reduced to no value, and it rendered `noValue`. Panel 20 shipped
        that way: `NO DISPATCHER` was the only string it could display, on a healthy
        dispatcher as readily as on a dead one -- the single distinction that panel exists to
        draw. Found on the first dry run, 2026-08-17.

        The test above could not catch it, and that is the point of having both: the mappings
        were correct the whole time, and unreachable.

        Written over every stat rather than panel 20 alone, and over the string fields
        `state.py` actually emits rather than a hardcoded name, so a future string stat is
        covered without anyone remembering this.
        """
        plan = json.loads((DASHBOARDS / "alphaess-battery-plan.json").read_text())
        strings = {k for k, v in published_field_values().items() if isinstance(v, str)}
        assert strings, "no string fields found -- this guard would pass vacuously"

        checked = 0
        for panel in plan["panels"]:
            if panel.get("type") not in ("stat", "gauge"):
                continue
            query = " ".join(t.get("query", "") for t in panel.get("targets", []))
            reads = {s for s in strings if f'_field == "{s}"' in query}
            if not reads:
                continue
            checked += 1
            picked = panel["options"]["reduceOptions"]["fields"]
            assert picked == "/.*/", (
                f"{panel.get('title')!r} reduces string field(s) {sorted(reads)} with "
                f"fields={picked!r}. Auto selects numeric fields only, so this panel renders "
                f"its noValue text in every state, including a healthy one")
        assert checked, "no stat panel reads a string field -- the guard found nothing"

    def test_a_string_stat_reduces_exactly_one_column(self):
        """`/.*/` means ALL fields, and Flux hands Grafana more than the one you asked for.

        The fix above traded a panel that showed nothing for a panel that showed too much:
        `_time` came back as a second tile, red, because no value mapping matches a timestamp
        and red is the base step -- and the `sys_sn` tag hung itself off the action's label.
        Seen on the dry run, 2026-08-17, one deploy after the first fix.

        So the two halves are a pair, and this asserts the half the previous test cannot see:
        having widened the picker to all columns, the query must return only the one.
        """
        checked = 0
        for dashboard, title, panel in _stat_panels():
            if panel["options"]["reduceOptions"]["fields"] != "/.*/":
                continue
            checked += 1
            for tgt in panel.get("targets", []):
                assert 'keep(columns: ["_value"])' in tgt.get("query", ""), (
                    f"{title!r} in {dashboard} reduces every field but its query still "
                    f"returns _time and the tags. Each renders as its own tile, and a "
                    f"timestamp matches no mapping, so it takes the base threshold colour")
        assert checked, "no stat reduces /.*/ -- the guard found nothing"

    def test_no_command_live_does_not_render_as_a_fault(self):
        """A panel that is red all day is a panel you stop reading.

        `Command expires in` yields nothing whenever no command is live -- the resting state,
        and the whole of a dry-run day. Grafana colours `noValue` with the BASE threshold
        step, so a red base painted that normal state as a full-width red `No data`, on the
        one panel whose red is meant to mean the battery is sixty seconds from acting
        unsupervised. The panel's own description called the state normal while the panel
        called it a fault.

        The clamp is part of the same fix, not a separate tidy-up: `expires_at` is fixed while
        `now()` advances, so a stalled loop drives this negative, and negative would land back
        on the now-neutral base step and go grey in exactly the failure being watched for.
        Neutral base and floor-at-zero only hold together.
        """
        panel = next(p for _, t, p in _stat_panels() if t == "Command expires in")
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        base = next(s for s in steps if s["value"] is None)
        assert base["color"] != "red", (
            "the base step colours noValue, and noValue here means 'nothing is dispatching' "
            "-- a normal rest, not a fault")
        assert any(s["color"] == "red" and s["value"] == 0 for s in steps), (
            "red has to start somewhere: a command with no time left on it is the fault this "
            "panel exists to show")
        query = " ".join(t.get("query", "") for t in panel.get("targets", []))
        assert "if r._value < 0.0 then 0.0 else r._value" in query, (
            "without the floor, a stalled loop counts past zero into the neutral base step "
            "and the panel goes quiet precisely when it should be shouting")


class TestPanelFive:
    """The third series is what makes the chart diagnostic: the gap between planned and
    commanded is a dispatcher bug, the gap between commanded and actual is delivery error."""

    def _panel(self):
        plan = json.loads((DASHBOARDS / "alphaess-battery-plan.json").read_text())
        return next(p for p in plan["panels"]
                    if p.get("title") == "Planned SoC vs actual SoC")

    def test_it_carries_a_commanded_series(self):
        queries = " ".join(t["query"] for t in self._panel()["targets"])
        assert '"commanded"' in queries

    def test_the_commanded_series_is_restricted_to_live_mode_2_commands(self):
        """0x0886 keeps its last value through a hold and through a release -- a Mode 3 hold
        writes no target at all. Plotting the register unconditionally would draw a confident
        flat line at a target nothing is driving toward."""
        q = next(t["query"] for t in self._panel()["targets"]
                 if MEASUREMENT in t.get("query", ""))
        assert "dispatch_active != 0" in q
        assert "mode == 2" in q

    def test_the_idle_stretches_are_nulled_rather_than_filtered_away(self):
        """The restriction above has to leave a datapoint behind saying "nothing commanded
        here". Dropping the rows instead gives a stepAfter line no points to step through,
        and Grafana joins the two ends -- redrawing the exact flat line the restriction
        exists to prevent, which is a worse failure than not having the series at all."""
        q = next(t["query"] for t in self._panel()["targets"]
                 if MEASUREMENT in t.get("query", ""))
        assert "debug.null" in q
        assert 'import "internal/debug"' in q, "debug.null needs its import to be in the query"
        assert "filter(fn: (r) => r.dispatch_active" not in q, (
            "the live-command test must null the value, not filter the row away")

    def test_the_series_has_its_own_override(self):
        """Without a byName override the series falls back to panel defaults -- filled area
        on the SoC axis, indistinguishable from the planned line."""
        names = {o["matcher"]["options"]
                 for o in self._panel()["fieldConfig"]["overrides"]}
        assert "commanded" in names
