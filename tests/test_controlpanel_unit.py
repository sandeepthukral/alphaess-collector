"""Unit tests for controlpanel's own logic -- confirmation phrases, CSRF enforcement,
argument validation, and the live-toggle no-op/missing-container paths.

None of this had a single test before this file: everything here is a pure function or a
short-circuit gating a live write to the inverter (dispatch's `DISPATCH_LIVE`), reachable
only through `tests/test_controlpanel_env_completeness.py` and
`tests/test_compose_env_guards.py`, which check the surrounding compose/env plumbing but
never import controlpanel's own code.

Needs INFLUX_URL/INFLUX_TOKEN_CONTROLPANEL/HOST_REPO_PATH set before `app`/`docker_actions`
import (module-level `os.environ[...]` reads) -- set via `setdefault` below so this doesn't
clobber a real value if one is ever set in CI. No real Influx or Docker socket is touched:
every test that exercises a route patches `docker_actions`/`audit`/the submission lookup.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("INFLUX_URL", "http://localhost:8086")
os.environ.setdefault("INFLUX_TOKEN_CONTROLPANEL", "test-token")
os.environ.setdefault("HOST_REPO_PATH", str(REPO))
# app.py's mutating routes flock a file under /data (the real container's writable volume)
# to serialize actions across gunicorn's worker processes -- not writable/present here.
os.environ.setdefault("CONTROLPANEL_LOCK_FILE",
                       str(Path(tempfile.gettempdir()) / "controlpanel-test.lock"))

import app as controlpanel_app  # noqa: E402
import backfill_actions  # noqa: E402
import docker_actions  # noqa: E402

# ---- backfill_actions: date/month validators, boundary-strict ---------------------------

@pytest.mark.parametrize("value", ["2026-01-01", "2026-12-31"])
def test_valid_date_accepted(value):
    assert backfill_actions._validate_date(value, "start") == value


@pytest.mark.parametrize("value", [
    "2026-1-1", "not-a-date", "2026-01-01 ", " 2026-01-01",
    "2026-01-01\n",  # `$` matches before a trailing newline -- \Z must not
    "2026-01-01x",
])
def test_invalid_date_rejected(value):
    with pytest.raises(backfill_actions.InvalidArgument):
        backfill_actions._validate_date(value, "start")


@pytest.mark.parametrize("value", ["2026-01", "2026-12"])
def test_valid_month_accepted(value):
    assert backfill_actions._validate_month(value, "month") == value


@pytest.mark.parametrize("value", ["2026-1", "2026-01\n", "2026-01x", "not-a-month"])
def test_invalid_month_rejected(value):
    with pytest.raises(backfill_actions.InvalidArgument):
        backfill_actions._validate_month(value, "month")


def test_mijnbatterij_monthly_requires_at_least_one_month():
    with pytest.raises(backfill_actions.InvalidArgument):
        backfill_actions.mijnbatterij_monthly([])


# ---- app: confirmation phrase ------------------------------------------------------------

def test_confirmation_phrase_go_live():
    assert controlpanel_app._confirmation_phrase(True) == "MAKE DISPATCH LIVE"


def test_confirmation_phrase_dry_run():
    assert controlpanel_app._confirmation_phrase(False) == "MAKE DISPATCH DRY-RUN"


# ---- app: CSRF enforcement, through the real Flask test client --------------------------

@pytest.fixture
def client():
    controlpanel_app.app.config.update(TESTING=True)
    return controlpanel_app.app.test_client()


def _get_csrf_token(client) -> str:
    """A GET establishes the session's csrf_token via the context processor."""
    with patch.object(docker_actions, "dispatch_status",
                       return_value={"exists": False, "live": None, "error": "n/a"}), \
         patch.object(controlpanel_app, "_latest_mijnbatterij_submission", return_value=None):
        client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def test_post_without_csrf_token_is_rejected(client):
    resp = client.post("/api/dispatch/start", data={})
    assert resp.status_code == 400


def test_post_with_wrong_csrf_token_is_rejected(client):
    _get_csrf_token(client)
    resp = client.post("/api/dispatch/start", data={"csrf_token": "wrong"})
    assert resp.status_code == 400


def test_post_with_correct_csrf_token_is_accepted(client):
    token = _get_csrf_token(client)
    with patch.object(docker_actions, "start_dispatch") as start:
        start.return_value = docker_actions.ActionResult(ok=True, stdout="", stderr="",
                                                           returncode=0)
        resp = client.post("/api/dispatch/start", data={"csrf_token": token})
    assert resp.status_code == 302


# ---- app: /api/live gating (no-op, missing container) ------------------------------------

def test_live_toggle_is_a_noop_when_already_in_the_requested_state(client):
    token = _get_csrf_token(client)
    status = {"exists": True, "live": True, "running": True, "started_at": None}
    with patch.object(docker_actions, "dispatch_status", return_value=status), \
         patch.object(docker_actions, "set_dispatch_live") as set_live, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    set_live.assert_not_called()
    assert resp.status_code == 302
    assert log_toggle.call_args.kwargs["accepted"] is True


def test_live_toggle_rejects_a_missing_dispatch_container(client):
    token = _get_csrf_token(client)
    status = {"exists": False, "live": None, "error": "no such container"}
    with patch.object(docker_actions, "dispatch_status", return_value=status), \
         patch.object(docker_actions, "set_dispatch_live") as set_live, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    set_live.assert_not_called()
    assert resp.status_code == 409
    assert log_toggle.call_args.kwargs["accepted"] is False


def test_live_toggle_rejects_wrong_confirmation_text(client):
    token = _get_csrf_token(client)
    status = {"exists": True, "live": False, "running": True, "started_at": None}
    with patch.object(docker_actions, "dispatch_status", return_value=status), \
         patch.object(docker_actions, "set_dispatch_live") as set_live, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "wrong phrase",
        })
    set_live.assert_not_called()
    assert resp.status_code == 400
    assert log_toggle.call_args.kwargs["accepted"] is False


def test_live_toggle_audits_true_outcome_not_compose_exit_code(client):
    """set_dispatch_live()'s own client can time out (ok=False) while the daemon-side
    recreate still lands -- the audit has to reflect what dispatch_status() reads back
    afterwards, not the compose call's exit code. See app.py's api_live()."""
    token = _get_csrf_token(client)
    before = {"exists": True, "live": False, "running": True, "started_at": None}
    after = {"exists": True, "live": True, "running": True, "started_at": None}
    with patch.object(docker_actions, "dispatch_status", side_effect=[before, after]), \
         patch.object(docker_actions, "set_dispatch_live",
                       return_value=docker_actions.ActionResult(
                           ok=False, stdout="", stderr="timed out after 180s", returncode=-1)), \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    assert resp.status_code == 302
    assert log_toggle.call_args.kwargs["accepted"] is True


def test_live_toggle_polls_past_a_slow_daemon_side_recreate(client):
    """The very first status read after a timed-out compose call can still show the OLD
    state if the daemon just hasn't finished yet -- _poll_dispatch_status_until must keep
    reading instead of deciding failure on that first stale read."""
    token = _get_csrf_token(client)
    stale = {"exists": True, "live": False, "running": True, "started_at": None}
    still_stale = {"exists": True, "live": False, "running": True, "started_at": None}
    achieved = {"exists": True, "live": True, "running": True, "started_at": None}
    with patch.object(docker_actions, "dispatch_status",
                       side_effect=[stale, still_stale, achieved]), \
         patch.object(docker_actions, "set_dispatch_live",
                       return_value=docker_actions.ActionResult(
                           ok=False, stdout="", stderr="timed out after 180s", returncode=-1)), \
         patch.object(controlpanel_app.time, "sleep") as sleep, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    assert sleep.call_count == 1  # one retry needed before "achieved" showed up
    assert resp.status_code == 302
    assert log_toggle.call_args.kwargs["accepted"] is True


def test_live_toggle_rejects_when_dispatch_is_stopped(client):
    """--force-recreate starts the container regardless of prior state -- toggling while
    stopped would silently start dispatch back up, which an operator who stopped it
    deliberately (e.g. mid-incident) would not expect from a page about live/dry-run mode."""
    token = _get_csrf_token(client)
    status = {"exists": True, "live": False, "running": False, "started_at": None}
    with patch.object(docker_actions, "dispatch_status", return_value=status), \
         patch.object(docker_actions, "set_dispatch_live") as set_live, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    set_live.assert_not_called()
    assert resp.status_code == 409
    assert log_toggle.call_args.kwargs["accepted"] is False


def test_concurrent_mutating_actions_are_rejected_while_one_is_in_progress(client):
    """flock is the only thing that can catch this: two requests (from two gunicorn worker
    PROCESSES, or just two browser tabs) both passing every earlier check and both reaching
    docker_actions.set_dispatch_live() at once. Simulated here by holding the same lock file
    open and exclusively locked before the request comes in. Also: this rejection must
    still be audited, same as every other rejection path -- the loser's attempt should not
    vanish from controlpanel_audit just because it lost a race for the lock."""
    import fcntl
    token = _get_csrf_token(client)
    status = {"exists": True, "live": False, "running": True, "started_at": None}
    with open(controlpanel_app._LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(docker_actions, "dispatch_status", return_value=status), \
             patch.object(docker_actions, "set_dispatch_live") as set_live, \
             patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
            resp = client.post("/api/live", data={
                "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
            })
        fcntl.flock(held, fcntl.LOCK_UN)
    set_live.assert_not_called()
    assert resp.status_code == 409
    log_toggle.assert_called_once()
    assert log_toggle.call_args.kwargs["accepted"] is False


def test_live_toggle_skips_the_poll_when_env_is_unconfigured(client):
    """EnvUnconfigured means set_dispatch_live() never ran anything -- polling
    dispatch_status() afterward would waste ~30s and then report the misleading "did not
    end up in the requested state" instead of the real, already-known reason."""
    token = _get_csrf_token(client)
    status = {"exists": True, "live": False, "running": True, "started_at": None}
    with patch.object(docker_actions, "dispatch_status", return_value=status), \
         patch.object(docker_actions, "set_dispatch_live",
                       side_effect=docker_actions.EnvUnconfigured("still a placeholder")), \
         patch.object(controlpanel_app.time, "sleep") as sleep, \
         patch.object(controlpanel_app.audit, "log_dispatch_live_toggle") as log_toggle:
        resp = client.post("/api/live", data={
            "csrf_token": token, "target": "live", "confirmation": "MAKE DISPATCH LIVE",
        })
    sleep.assert_not_called()
    assert resp.status_code == 409
    assert log_toggle.call_args.kwargs["accepted"] is False
    assert "still a placeholder" in log_toggle.call_args.kwargs["reason"]


# ---- docker_actions: refuse a go-live toggle against an unedited env file ----------------

def test_set_dispatch_live_refuses_when_env_still_has_placeholder_values(tmp_path):
    env_file = tmp_path / "controlpanel.env"
    env_file.write_text(
        "ALPHAESS_SYS_SN=your_system_serial_number\n"
        "INFLUX_TOKEN_DISPATCH=read_planning_read_write_alphaess\n"
    )
    with patch.object(docker_actions, "CONTROLPANEL_ENV_FILE", str(env_file)), \
         pytest.raises(docker_actions.EnvUnconfigured, match=r"DEPLOY\.md"):
        docker_actions.set_dispatch_live(True)


@pytest.mark.parametrize("value", [
    "your_system_serial_number ",       # trailing space
    " your_system_serial_number",       # leading space
    '"your_system_serial_number"',      # quoted
    "'your_system_serial_number'",      # single-quoted
])
def test_set_dispatch_live_placeholder_check_tolerates_whitespace_and_quoting(tmp_path, value):
    env_file = tmp_path / "controlpanel.env"
    env_file.write_text(f"ALPHAESS_SYS_SN={value}\n")
    with patch.object(docker_actions, "CONTROLPANEL_ENV_FILE", str(env_file)), \
         pytest.raises(docker_actions.EnvUnconfigured):
        docker_actions.set_dispatch_live(True)


def test_set_dispatch_live_allows_dry_run_even_with_placeholder_values(tmp_path):
    """The guard only applies to going LIVE -- dry-run stays safe regardless of whether
    deploy/controlpanel.env was ever filled in."""
    env_file = tmp_path / "controlpanel.env"
    env_file.write_text("ALPHAESS_SYS_SN=your_system_serial_number\n")
    with patch.object(docker_actions, "CONTROLPANEL_ENV_FILE", str(env_file)), \
         patch.object(docker_actions, "OVERRIDE_FILE", str(tmp_path / "override.yml")), \
         patch.object(docker_actions, "_run") as run:
        run.return_value = docker_actions.ActionResult(ok=True, stdout="", stderr="",
                                                         returncode=0)
        result = docker_actions.set_dispatch_live(False)
    assert result.ok
    run.assert_called_once()


def test_set_dispatch_live_proceeds_once_placeholders_are_replaced(tmp_path):
    env_file = tmp_path / "controlpanel.env"
    env_file.write_text(
        "ALPHAESS_SYS_SN=ES500123456789\n"
        "INFLUX_TOKEN_DISPATCH=a-real-per-install-token\n"
    )
    with patch.object(docker_actions, "CONTROLPANEL_ENV_FILE", str(env_file)), \
         patch.object(docker_actions, "OVERRIDE_FILE", str(tmp_path / "override.yml")), \
         patch.object(docker_actions, "_run") as run:
        # Called twice when going live: once by _heartbeat_regression_reason()'s own
        # `docker inspect` (empty stdout here, so it finds nothing to compare and allows
        # the toggle through), once for the actual compose recreate.
        run.return_value = docker_actions.ActionResult(ok=True, stdout="", stderr="",
                                                         returncode=0)
        result = docker_actions.set_dispatch_live(True)
    assert result.ok
    assert run.call_count == 2


def test_set_dispatch_live_refuses_a_heartbeat_url_regression(tmp_path):
    """The container currently running has a real Kuma URL configured; the env file about
    to be used for the recreate would blank it out -- must be refused, not silently
    applied."""
    env_file = tmp_path / "controlpanel.env"
    env_file.write_text(
        "ALPHAESS_SYS_SN=ES500123456789\n"
        "INFLUX_TOKEN_DISPATCH=a-real-per-install-token\n"
        "SOC_FLOOR_HEARTBEAT_URL=\n"
    )
    inspect_result = docker_actions.ActionResult(
        ok=True, stdout=json.dumps([{
            "Config": {"Env": ["SOC_FLOOR_HEARTBEAT_URL=https://kuma.example/push/abc"]},
        }]), stderr="", returncode=0)
    with patch.object(docker_actions, "CONTROLPANEL_ENV_FILE", str(env_file)), \
         patch.object(docker_actions, "_run", return_value=inspect_result), \
         pytest.raises(docker_actions.EnvUnconfigured, match="SOC_FLOOR_HEARTBEAT_URL"):
        docker_actions.set_dispatch_live(True)


def test_parse_env_file_strips_whitespace_and_quotes():
    parsed = docker_actions._parse_env_file(
        'A=plain\n'
        'B = padded \n'
        'C="quoted"\n'
        "D='single-quoted'\n"
        "# comment\n"
        "\n"
    )
    assert parsed == {"A": "plain", "B": "padded", "C": "quoted", "D": "single-quoted"}
