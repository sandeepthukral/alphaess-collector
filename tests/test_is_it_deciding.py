"""`scripts/is-it-deciding.py` -- the one-screen answer, and the lines it prints.

WHY THIS FILE EXISTS. The script is the thing you run when the question is "did I just break
it", so its output is read under exactly the conditions where a confusing line does the most
damage: something looks wrong, and the script is the instrument. It had no tests at all, and
its two most load-bearing lines are both about ABSENT fields -- a missing `slot_action` and a
missing `action` -- which is the class of bug that reads as a broken script rather than as
the condition it is reporting.

`report()` takes a dict and prints, so the cases below are the dict shapes `state.py`
actually publishes. The rest of the script is an Influx query and an argument parser.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script

deciding = load_script("is_it_deciding", "is-it-deciding.py")


def state(**over) -> dict:
    """A healthy live tick, seconds old, with fields overridable per case."""
    base = {
        "_time": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
        "slot_action": "hold",
        "action": "no dispatch",
        "setpoint_w": 0,
        "dispatch_active": 1,
    }
    return base | over


def output(capsys, **over) -> tuple[str, int]:
    code = deciding.report(state(**over))
    return capsys.readouterr().out, code


class TestAnUnreadableInverter:
    """A degraded tick publishes its DECISION and no register readback at all.

    `state.py:129-141` is explicit that the honest report of an unreadable inverter is a
    missing field, not a stale one -- so `action` and `setpoint_w` are simply absent.
    """

    def test_it_names_the_error_instead_of_printing_none(self, capsys):
        out, _ = output(capsys, read_error="timed out", action=None, setpoint_w=None)
        assert "UNREADABLE -- timed out" in out
        # The line this replaced. `None / None W` reads as a broken script, which sends you
        # to debug the wrong thing at the exact moment the inverter is unreachable.
        assert "None / None W" not in out

    def test_the_decision_line_survives_a_failed_readback(self, capsys):
        """The question the script is named after still has an answer.

        The loop decided; only the readback failed. Losing the decision line here would turn
        the fail-safe working into a screen that looks like nothing happened.
        """
        out, code = output(capsys, read_error="timed out", action=None, setpoint_w=None)
        assert "decision   hold" in out
        assert "the loop decided anyway" in out
        assert code == 0

    def test_it_still_says_which_mode_it_was_in(self, capsys):
        out, _ = output(capsys, read_error="timed out", dispatch_active=0)
        assert "(dry run)" in out


class TestTheOrdinaryTick:
    def test_a_readable_inverter_prints_the_readback(self, capsys):
        out, code = output(capsys)
        assert "readback   no dispatch / 0 W (live)" in out
        assert "UNREADABLE" not in out
        assert code == 0

    def test_dry_run_says_why_the_readback_never_changes(self, capsys):
        out, _ = output(capsys, dispatch_active=0)
        assert "dry run writes nothing, so this never changes" in out

    def test_no_slot_is_reported_as_normal_and_not_as_trouble(self, capsys):
        """`slot_action` is CONDITIONAL and its absence is not a fault.

        `state.py:113` writes it only while a slot is active, so a healthy dispatcher outside
        the plan's horizon writes a point without it. Crying wolf here is the mistake panel
        23 shipped with `expires_at` before it was gated.
        """
        out, code = output(capsys, slot_action=None)
        assert "(no slot)" in out
        assert "normal outside the horizon" in out
        assert code == 0


class TestTheVerdictLine:
    def test_a_recent_tick_is_deciding(self, capsys):
        out, code = output(capsys)
        assert "DECIDING" in out
        assert code == 0

    def test_silence_past_the_threshold_is_stalled(self, capsys):
        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=deciding.GAP_S + 60)
        out, code = output(capsys, _time=old)
        assert "STALLED" in out
        # Exit code, not just the word: `--watch` and a shell both read this.
        assert code == 1

    def test_no_point_at_all_is_the_dead_loop_case(self, capsys):
        code = deciding.report(None)
        out = capsys.readouterr().out
        assert "NOT DECIDING" in out
        assert "docker compose logs" in out
        assert code == 2


class TestThePlanLine:
    @pytest.mark.parametrize("offset", ["Z", "+02:00"], ids=["utc", "local-offset"])
    def test_a_stale_plan_is_flagged_whichever_spelling_the_tag_uses(self, capsys, offset):
        """Tags written before 2026-07-30 carry `+02:00` where later ones carry `Z`.

        Two hours apart, which is the whole staleness threshold -- so a naive read of the
        older spelling clears a plan that is in fact stale.
        """
        stamp = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=deciding.STALE_PLAN_S + 3600)
        tag = stamp.astimezone(
            dt.UTC if offset == "Z" else dt.timezone(dt.timedelta(hours=2))
        ).isoformat().replace("+00:00", "Z")
        out, _ = output(capsys, plan_run=tag)
        assert "STALE, the translator has stopped" in out

    def test_a_fresh_plan_is_not_flagged(self, capsys):
        tag = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)).isoformat()
        out, _ = output(capsys, plan_run=tag.replace("+00:00", "Z"))
        assert "STALE" not in out
        assert "h old)" in out
