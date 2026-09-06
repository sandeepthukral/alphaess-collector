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

import os
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent

os.environ.setdefault("INFLUX_URL", "http://localhost:8086")
os.environ.setdefault("INFLUX_TOKEN_CONTROLPANEL", "test-token")
os.environ.setdefault("HOST_REPO_PATH", str(REPO))

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
