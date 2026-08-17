"""The `planning` bucket boundary. PLAN-repo-seams.md Part 2b.

`battery-planning` writes the `planning` bucket; this repo reads it, from two places that do
not know about each other -- `dispatch/plan.py:from_influx` on the control path, and the
Grafana dashboards. Neither repo's tests cross the boundary, so a field rename over there
lands here as a blank panel and, once dispatch is live, as a silent stop.

`tests/fixtures/planning_schema.json` is that boundary written down. These tests do not
query InfluxDB -- CI has no NAS, and a test that skips without one guards nothing. They
assert the half that is checkable offline: that nothing in this repo reads a field the
fixture does not name. Keeping the fixture honest about what the DATABASE holds is a
separate act, done by re-querying it and recorded in `verified_against_live_bucket`.

So the failure this catches is "someone added a read without declaring it", which is the
common case and the one that silently widens the dependency. The failure it cannot catch is
"the planner renamed a field it already had"; that one is caught at runtime, by
`from_influx` raising with the field name and monitor #2 carrying it -- which is why both
halves exist.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from plan import REQUIRED_FIELDS

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = FIXTURES / "planning_schema.json"
GRAFANA = Path(__file__).parent.parent / "grafana"

SCHEMA = json.loads(SCHEMA_PATH.read_text())
MEASUREMENTS = SCHEMA["measurements"]

# `from(bucket: "x")` starts a pipeline; everything until the next one belongs to bucket x.
# Splitting on it is what keeps a query that reads BOTH buckets -- the price panels union
# `alphaess`'s recorded prices with `planning`'s forecast -- from being read as if every
# field in it came from `planning`.
FROM_BUCKET = re.compile(r'from\(\s*bucket:\s*"([^"]+)"\s*\)')
MEASUREMENT_EQ = re.compile(r'_measurement\s*==\s*"([^"]+)"')
FIELD_EQ = re.compile(r'_field\s*==\s*"([^"]+)"')


def declared_fields(measurement: str) -> set[str]:
    m = MEASUREMENTS[measurement]
    return set(m["required_fields"]) | set(m["optional_fields"])


def _queries(obj, acc: list[str]) -> None:
    """Every Flux string in a dashboard, wherever it is nested."""
    if isinstance(obj, dict):
        q = obj.get("query")
        if isinstance(q, str):
            acc.append(q)
        for v in obj.values():
            _queries(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _queries(v, acc)


def planning_segments(flux: str) -> list[tuple[set[str], set[str]]]:
    """(measurements, fields) for each `planning` pipeline in one query."""
    marks = [(m.start(), m.group(1)) for m in FROM_BUCKET.finditer(flux)]
    out = []
    for i, (pos, bucket) in enumerate(marks):
        if bucket != "planning":
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(flux)
        segment = flux[pos:end]
        out.append((set(MEASUREMENT_EQ.findall(segment)), set(FIELD_EQ.findall(segment))))
    return out


def dashboards_reading_planning() -> list[tuple[Path, list[tuple[set[str], set[str]]]]]:
    found = []
    for path in sorted(GRAFANA.glob("alphaess-*.json")):
        acc: list[str] = []
        _queries(json.loads(path.read_text()), acc)
        segments = [seg for q in acc for seg in planning_segments(q)]
        if segments:
            found.append((path, segments))
    return found


class TestTheFixtureItself:
    def test_it_names_the_bucket_and_when_it_was_checked(self):
        assert SCHEMA["bucket"] == "planning"
        assert SCHEMA["verified_against_live_bucket"], (
            "the fixture must record when it was last checked against the real bucket -- "
            "without that it is a guess with the authority of a committed file")

    def test_every_measurement_says_why_this_repo_reads_it(self):
        """A field nobody can justify is one nobody will dare delete."""
        for name, m in MEASUREMENTS.items():
            assert m["why"], f"{name} has no reason recorded"

    def test_required_and_optional_do_not_overlap(self):
        for name, m in MEASUREMENTS.items():
            both = set(m["required_fields"]) & set(m["optional_fields"])
            assert not both, f"{name}: {sorted(both)} listed as both required and optional"

    def test_the_load_forecast_is_not_declared_anywhere(self):
        """`plan.load_forecast_wh` is the household's occupancy signal at 15-minute
        resolution, and this repo is public. It exists in the bucket; depending on it here
        is the thing to prevent. `dispatch/corpus.py` drops it at the parse boundary for the
        same reason -- this is the same rule, one layer out."""
        for name in MEASUREMENTS:
            assert "load_forecast_wh" not in declared_fields(name), (
                f"{name} declares load_forecast_wh -- that is occupancy data in a public repo")


class TestTheTranslatorReadsOnlyDeclaredFields:
    """`dispatch/plan.py:from_influx` is the control path. Its reads are the subset that
    matters most: a field it wants and the fixture does not name is an undeclared dependency
    that stops dispatch when the other repo drops it."""

    # Every `v[...]` / `v.get(...)` key `from_influx` pulls off a record, read from the
    # source rather than restated, so adding a read there fails here rather than drifting.
    def _fields_from_influx_reads(self) -> set[str]:
        src = (Path(__file__).parent.parent / "dispatch" / "plan.py").read_text()
        body = src[src.index("def from_influx("):src.index("def iso_z(")]
        keys = set(re.findall(r'v\[\s*"([a-z_]+)"\s*\]', body))
        keys |= set(re.findall(r'v\.get\(\s*"([a-z_]+)"', body))
        return keys

    def test_the_reads_are_a_subset_of_the_fixture(self):
        reads = self._fields_from_influx_reads()
        declared = declared_fields("plan") | set(MEASUREMENTS["plan"]["tags"])
        undeclared = reads - declared
        assert not undeclared, (
            f"from_influx reads {sorted(undeclared)}, which "
            f"{SCHEMA_PATH.name} does not declare. Either declare them -- and check the "
            f"planner really writes them -- or stop reading them.")

    def test_it_actually_found_the_reads(self):
        """Guards the extraction above. If `from_influx` is refactored into a shape this
        regex cannot see, the subset test would pass by reading nothing at all."""
        reads = self._fields_from_influx_reads()
        assert len(reads) >= len(REQUIRED_FIELDS), (
            f"only found {sorted(reads)} -- the extraction has stopped working, and the "
            f"test above is now vacuous")

    def test_every_required_field_is_declared_as_required(self):
        """`REQUIRED_FIELDS` is what `from_influx` refuses to run without, so the fixture
        must not describe any of them as optional."""
        assert set(REQUIRED_FIELDS) <= set(MEASUREMENTS["plan"]["required_fields"])

    def test_the_plan_run_tag_is_declared(self):
        """Not a field, and easy to forget for that reason -- but `newest_by_interval` and
        every 'plan in force' readout depend on it."""
        assert "plan_run" in MEASUREMENTS["plan"]["tags"]


class TestDashboardsReadOnlyDeclaredFields:
    def test_some_dashboard_actually_reads_the_planning_bucket(self):
        """If the extraction breaks, every test below passes over an empty list."""
        found = dashboards_reading_planning()
        assert found, "no dashboard appears to read the planning bucket -- extraction broken"
        assert sum(len(s) for _, s in found) >= 10

    def test_every_measurement_they_query_is_declared(self):
        for path, segments in dashboards_reading_planning():
            for measurements, _ in segments:
                undeclared = measurements - set(MEASUREMENTS)
                assert not undeclared, (
                    f"{path.name} queries measurement(s) {sorted(undeclared)} in the "
                    f"planning bucket, which {SCHEMA_PATH.name} does not declare")

    def test_every_field_they_query_is_declared(self):
        """Checked against the union of the fields of the measurements that pipeline names.

        The union rather than an exact pairing: a single `filter()` can name a measurement
        and a field in one boolean expression, and matching each field to its own
        measurement by regex would be guesswork. The union still catches the case this
        exists for -- a field nobody declared -- and never fails on a correct dashboard.
        """
        for path, segments in dashboards_reading_planning():
            for measurements, fields in segments:
                allowed: set[str] = set()
                for m in measurements & set(MEASUREMENTS):
                    allowed |= declared_fields(m)
                undeclared = fields - allowed
                assert not undeclared, (
                    f"{path.name} reads {sorted(undeclared)} from the planning bucket "
                    f"(measurement(s) {sorted(measurements)}), which {SCHEMA_PATH.name} "
                    f"does not declare")


class TestTheExtractorItself:
    """The tests above are only as good as this. Each case is one drawn from a real
    dashboard query, so a regex that stops matching fails here rather than going quiet."""

    def test_it_ignores_a_pipeline_from_another_bucket(self):
        """The price panels union `alphaess`'s recorded prices with `planning`'s forecast.
        Reading that as one bucket would attribute `market_price` -- an `alphaess`
        measurement -- to the planning schema, and demand it be declared here."""
        flux = '''
        past = from(bucket: "alphaess")
          |> filter(fn: (r) => r._measurement == "market_price" and r._field == "market_price")
        future = from(bucket: "planning")
          |> filter(fn: (r) => r._measurement == "plan" and r._field == "price_market")
        '''
        segments = planning_segments(flux)
        assert len(segments) == 1
        measurements, fields = segments[0]
        assert measurements == {"plan"}
        assert fields == {"price_market"}

    def test_it_finds_both_pipelines_when_both_are_planning(self):
        flux = '''
        a = from(bucket: "planning")
          |> filter(fn: (r) => r._measurement == "plan" and r._field == "soc_wh")
        b = from(bucket: "planning")
          |> filter(fn: (r) => r._measurement == "app_setting" and r._field == "until_s")
        '''
        assert len(planning_segments(flux)) == 2

    def test_a_query_touching_no_planning_bucket_yields_nothing(self):
        assert planning_segments('from(bucket: "alphaess") |> filter(fn: (r) => true)') == []


class TestAnUndeclaredReadIsCaught:
    """The tests above pass today; these show they would fail tomorrow. Without this, a
    subset check that had quietly stopped extracting anything would look identical."""

    def test_an_undeclared_dashboard_field_is_rejected(self):
        _, fields = planning_segments(
            'from(bucket: "planning")\n'
            '  |> filter(fn: (r) => r._measurement == "plan" and r._field == "invented_wh")'
        )[0]
        assert fields - declared_fields("plan") == {"invented_wh"}

    def test_the_occupancy_field_would_be_rejected_too(self):
        """The specific read this boundary exists to keep out."""
        _, fields = planning_segments(
            'from(bucket: "planning")\n'
            '  |> filter(fn: (r) => r._measurement == "plan" '
            'and r._field == "load_forecast_wh")'
        )[0]
        assert fields - declared_fields("plan") == {"load_forecast_wh"}


@pytest.mark.parametrize("measurement", sorted(MEASUREMENTS))
def test_declared_fields_are_non_empty_somewhere(measurement):
    """A measurement declared with no fields at all is a leftover, not a dependency."""
    assert declared_fields(measurement) or MEASUREMENTS[measurement]["tags"], (
        f"{measurement} declares nothing -- drop it from the fixture")
