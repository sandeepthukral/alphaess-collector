"""The backoff cap and the pricing gap gate must stay compatible.

Nothing at runtime couples them, and the failure is silent in both directions.
The collector's backoff decides how long an outage silences it, so it decides
how big a gap a run of failed polls leaves in power_readings; pricing.py then
throws away any day whose largest gap exceeds PRICING_MAX_GAP_S. A cap raised
for API politeness therefore costs whole days of savings data, and nothing in
either module says so.

This is not hypothetical. With the previous 300 s cap, four days in 2026-07
carried gaps of 916-1051 s against a 1200 s gate -- each one failed poll below
losing the day entirely. See MIGRATION.md, "Follow-ups this migration
surfaced".
"""

import pytest

from collector import (
    DEFAULT_MAX_BACKOFF_S,
    backoff_seconds,
    gap_after_failures,
)
from pricing import MAX_GAP_S

INTERVAL = 30

# Consecutive failures the collector should survive without costing the day.
# Five is what the observed outages actually reached.
SURVIVABLE_FAILURES = 5


# --------------------------------------------------------------------------
# backoff_seconds
# --------------------------------------------------------------------------

def test_healthy_polls_wait_exactly_the_interval():
    assert backoff_seconds(INTERVAL, 0) == INTERVAL


def test_backoff_doubles_then_holds_at_the_cap():
    ladder = [backoff_seconds(INTERVAL, n) for n in range(1, 7)]
    assert ladder == [60, 120, 120, 120, 120, 120]


def test_the_cap_binds_regardless_of_how_long_the_outage_runs():
    """The exponent stops at 4, so nothing escapes the cap late in an outage."""
    assert backoff_seconds(INTERVAL, 500) == DEFAULT_MAX_BACKOFF_S


def test_a_cap_below_one_step_still_caps():
    assert backoff_seconds(INTERVAL, 1, max_backoff=45) == 45


# --------------------------------------------------------------------------
# gap_after_failures -- the ladder that decides whether a day survives
# --------------------------------------------------------------------------

def test_gap_ladder_at_the_shipped_cap():
    """30 + 60 + 120 * (k - 1)."""
    gaps = [gap_after_failures(INTERVAL, k) for k in range(1, 7)]
    assert gaps == [90, 210, 330, 450, 570, 690]


def test_a_clean_poll_leaves_only_one_interval():
    assert gap_after_failures(INTERVAL, 0) == INTERVAL


# --------------------------------------------------------------------------
# The coupling itself
# --------------------------------------------------------------------------

def test_surviving_a_realistic_outage_does_not_cost_the_day():
    """The invariant this module exists to protect."""
    gap = gap_after_failures(INTERVAL, SURVIVABLE_FAILURES)
    assert gap < MAX_GAP_S, (
        f"{SURVIVABLE_FAILURES} consecutive failures leave a {gap:.0f}s gap, "
        f"over the {MAX_GAP_S:.0f}s gate in pricing.py -- pricing would discard "
        f"the whole day. Lower DEFAULT_MAX_BACKOFF_S or raise PRICING_MAX_GAP_S "
        f"deliberately, not by accident.")


def test_there_is_real_headroom_beyond_the_observed_outages():
    """Not just passing: passing with room, so the next bad day is not a cliff.

    The 300 s cap failed this while still passing at five failures -- 1050 s
    against 1200 s. Sitting just under the gate is what made those four days
    dangerous rather than fine.
    """
    survivable = max(k for k in range(1, 100)
                     if gap_after_failures(INTERVAL, k) < MAX_GAP_S)
    assert survivable >= 2 * SURVIVABLE_FAILURES, (
        f"only {survivable} consecutive failures fit under the {MAX_GAP_S:.0f}s "
        f"gate; want at least twice the {SURVIVABLE_FAILURES} seen in practice")


def test_the_old_300s_cap_would_now_fail_this_suite():
    """Guards the regression directly: this is the setting that was shipped."""
    gap = gap_after_failures(INTERVAL, 6, max_backoff=300)
    assert gap > MAX_GAP_S


@pytest.mark.parametrize("interval", [10, 15, 30, 60])
def test_the_invariant_holds_at_every_supported_interval(interval):
    """POLL_INTERVAL_SECONDS is configurable; the gate is not per-interval."""
    assert gap_after_failures(interval, SURVIVABLE_FAILURES) < MAX_GAP_S
