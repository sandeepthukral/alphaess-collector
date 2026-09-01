"""`scripts/daily-mijnbatterij.sh` -- the nightly `--monthly` publication.

The script is three lines of shell around one piece of real logic: which months
to send. That logic lives in a `python -c` heredoc inside the script, where
nothing else can reach it, so these tests pull it back out and run it against a
fixed clock. The cases that matter are the boundaries -- the 1st of a month,
where "the month of yesterday" and "the month of today" differ and only the
former holds the day that just finished, and the year rollover, where a naive
`month - 1` produces month 0.

The coupling test is the other half. The script re-posts the previous month for
the first HEAL_DAYS days of a new one because `daily-savings.sh` can write a
day's `daily_cost` up to WINDOW_DAYS late; if that window is widened there and
not here, the late day is healed in InfluxDB and never published, which is
invisible from both ends.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import zoneinfo
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "daily-mijnbatterij.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_the_script_is_executable():
    """DSM Task Scheduler runs it by path; a non-executable file fails there and
    nowhere else, which is a bad place to find out."""
    assert os.access(SCRIPT, os.X_OK), f"chmod +x {SCRIPT.relative_to(REPO)}"


def test_deploy_md_says_how_to_schedule_it():
    deploy = (REPO / "DEPLOY.md").read_text(encoding="utf-8")
    assert "scripts/daily-mijnbatterij.sh" in deploy


def test_the_heal_window_matches_daily_savings():
    savings = (REPO / "scripts" / "daily-savings.sh").read_text(encoding="utf-8")
    window = int(re.search(r"^WINDOW_DAYS=(\d+)", savings, re.M).group(1))
    heal = int(re.search(r"^HEAL_DAYS=(\d+)", TEXT, re.M).group(1))
    assert heal == window, (
        f"daily-savings.sh heals days up to {window} days late, but this script stops "
        f"re-posting the previous month after {heal}. A day priced in the gap is written "
        f"to daily_cost and never published.")


def _months_on(today: dt.date, heal_days: int = 4) -> list[str]:
    """Run the script's own month-selection snippet with the clock pinned.

    The snippet's first line imports `datetime`, `zoneinfo` and `os` under short
    names; it is asserted and then dropped, so the names can be bound to a fake
    clock instead. Running the real text rather than a copy of it is the whole
    point -- a copy would keep passing after the script changed.
    """
    snippet = re.search(r'python -c "\n(.*?)\n"\)', TEXT, re.S).group(1)
    first, rest = snippet.split("\n", 1)
    assert first == "import datetime as d, zoneinfo as z, os", first

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.combine(today, dt.time(3, 30), tzinfo=tz)

    class FakeDatetimeModule:
        datetime = FrozenDateTime
        timedelta = dt.timedelta

    fake_os = SimpleNamespace(environ={"HEAL_DAYS": str(heal_days)})

    out = io.StringIO()
    with redirect_stdout(out):
        exec(rest, {"d": FakeDatetimeModule, "z": zoneinfo, "os": fake_os})
    return out.getvalue().split()


@pytest.mark.parametrize("today,expected", [
    # Mid-month: only the month in progress.
    ("2026-09-15", ["2026-09"]),
    # The 1st: yesterday is in LAST month, and that month's final day has just
    # had its daily_cost written. Sending "this month" here would send nothing.
    ("2026-09-01", ["2026-08"]),
    # Inside the heal window: both, previous month first so the month that can
    # still change is posted before the one that cannot.
    ("2026-09-02", ["2026-08", "2026-09"]),
    ("2026-09-05", ["2026-08", "2026-09"]),
    # Just past it.
    ("2026-09-06", ["2026-09"]),
    # Year rollover, where `month - 1` would be 0.
    ("2027-01-01", ["2026-12"]),
    ("2027-01-03", ["2026-12", "2027-01"]),
    # March 1st: February is 28 or 29 days and neither is assumed anywhere.
    ("2026-03-01", ["2026-02"]),
])
def test_which_months_get_posted(today, expected):
    assert _months_on(dt.date.fromisoformat(today)) == expected


def test_the_snippet_respects_heal_days():
    """The window is a variable in the script, so it has to reach the snippet --
    a hard-coded 4 inside the heredoc would pass every test above."""
    assert _months_on(dt.date(2026, 9, 6), heal_days=10) == ["2026-08", "2026-09"]
