"""Web control panel for the dispatcher and other console-only ops.

A friendlier front door to commands that already exist and already work -- not new dispatch
logic. Reachable on the LAN behind nginx basic auth (see nginx/controlpanel.conf); this app
itself is never published directly. See DEPLOY.md, "Control panel".
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import secrets
import time

import audit
import backfill_actions
import docker_actions
import reliability_view
from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from influxdb_client import InfluxDBClient

app = Flask(__name__)
# Generated once per container start, before gunicorn forks its workers, so every worker in
# this container shares it and can read each other's session cookies. Not persisted across
# restarts -- that's fine, a CSRF token only has to outlive the page it was rendered on, and
# nginx's basic auth (not this cookie) is what actually gates access.
app.secret_key = secrets.token_bytes(32)


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": _csrf_token()}


@app.before_request
def _check_csrf():
    # nginx basic auth is what actually gates access to this app; this only stops a
    # cross-site page from riding a logged-in browser's session to POST here, since the
    # confirmation phrases on /api/live are hardcoded constants an attacker can already guess.
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        # compare_digest on `str` requires both sides to be ASCII-only or it raises
        # TypeError -- a submitted value with a stray non-ASCII byte would 500 instead of
        # the intended 400. Comparing as bytes accepts anything.
        if not expected or not secrets.compare_digest(
            submitted.encode("utf-8"), expected.encode("utf-8")
        ):
            abort(400, description="Missing or invalid CSRF token -- reload the page and retry.")


class ActionInProgress(Exception):
    """Raised by `_exclusive_action()` when another mutating action already holds the lock."""


# All of start/stop/live-toggle/backfill/resubmit, serialized across BOTH gunicorn worker
# PROCESSES and every thread in each (--workers 2 --threads 4, controlpanel/Dockerfile). A
# `threading.Lock` only ever covers threads within one process -- with 2 worker processes a
# double-click, or two people using the panel at once, can land on two different processes
# that share no Python memory at all, so a Python-level lock would miss it entirely. `flock`
# on a file in the container's own writable /data volume is what actually synchronizes
# across processes on the same host. This matters most for the live toggle: two requests
# each independently reading `current_live != target_live` as true and both proceeding
# would race to write deploy/dispatch-live.override.yml and both run
# `compose ... --force-recreate dispatch`, with the final DISPATCH_LIVE value decided by
# whichever `docker compose` process happens to finish last -- not by either operator.
# Non-blocking (LOCK_NB): a request that finds the lock held fails fast with a clear error
# instead of silently queueing behind a backfill that can run for up to 30 minutes.
# Overridable so tests don't need to write to the container's real /data volume.
_LOCK_FILE = os.environ.get("CONTROLPANEL_LOCK_FILE", "/data/controlpanel.lock")


@contextlib.contextmanager
def _exclusive_action():
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    with open(_LOCK_FILE, "w") as lock_fp:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ActionInProgress(
                "Another action is already running -- wait for it to finish and retry."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)


INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "alphaess")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN_CONTROLPANEL"]

_influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_query_api = _influx.query_api()


def _latest_mijnbatterij_submission() -> dict | None:
    # `outcome` is a TAG on mijnbatterij_submit (collector/mijnbatterij.py), so `submitted`
    # and `status_code` each land in a SEPARATE table per distinct outcome value seen in the
    # window (e.g. one table for "ok", another for a stale "error" from hours earlier).
    # Worse: mijnbatterij.py only writes `status_code` on the "ok" path -- the error path
    # (mijnbatterij.py's `except` branch) writes `submitted` alone. A `pivot()` across both
    # fields therefore produces tables with DIFFERENT COLUMN SETS whenever both outcomes
    # appear in the window, and a following `group()` -- which requires every table it
    # merges to share a schema -- errors out on exactly that mix, which is precisely when
    # this widget matters most (the submitter is failing).
    #
    # Two simpler queries instead of one clever one: `submitted` is written on EVERY
    # attempt, so it alone is enough to find the single latest attempt's time and outcome,
    # with a query over a single field -- one schema, no collision possible. `status_code`
    # is then looked up only in the exact same instant, which is empty (not an error, just
    # no rows) on the error path.
    latest_flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -26h)
      |> filter(fn: (r) => r._measurement == "mijnbatterij_submit" and r._field == "submitted")
      |> group()
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    # Influx is not on the critical path for start/stop -- those act on the dispatch
    # container directly, over the Docker socket, with no Influx involved. Losing this
    # widget must never take the whole dashboard (and its start/stop buttons) down with it,
    # which is exactly when an operator needs them most. But a query failure (dead
    # InfluxDB, a revoked token, a broken Flux query after an edit) has to come back as a
    # DISTINCT state from "no submission" -- collapsing them into the same `None` had this
    # rendering as the positive claim "no submission in the last 26h", which sends whoever
    # is debugging a real Influx outage looking at the wrong service entirely.
    try:
        tables = _query_api.query(latest_flux)
    except Exception as e:
        return {"query_error": str(e)}
    latest = None
    for table in tables:
        for record in table.records:
            latest = record
            break
        if latest:
            break
    if latest is None:
        return None

    submitted_at = latest.get_time()
    status_code = None
    status_flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {submitted_at.isoformat()},
               stop: {(submitted_at + dt.timedelta(microseconds=1)).isoformat()})
      |> filter(fn: (r) => r._measurement == "mijnbatterij_submit" and r._field == "status_code")
      |> limit(n: 1)
    '''
    try:
        for table in _query_api.query(status_flux):
            for record in table.records:
                status_code = record.get_value()
                break
    except Exception:
        pass  # `submitted`/`outcome` are already known good; a broken second query here
              # must not blank out a result we already have.

    return {
        "time": submitted_at,
        "outcome": latest.values.get("outcome"),
        "submitted": latest.get_value(),
        "status_code": status_code,
    }


@app.route("/")
def dashboard():
    # No `is_it_deciding()` here on purpose -- it is a subprocess with its own 30s timeout
    # that shells out and queries InfluxDB, and calling it on every dashboard load (and
    # again below on every failed start/stop) made the busiest page in the app, including
    # its own Start/Stop buttons, as slow and as Influx-dependent as the worst case of a
    # script most operators only need to run occasionally. It has its own page, /reliability,
    # with a button to run it on demand.
    status = docker_actions.dispatch_status()
    submission = _latest_mijnbatterij_submission()
    return render_template("dashboard.html", status=status, submission=submission)


def _dashboard_error(error: str, code: int = 500):
    status = docker_actions.dispatch_status()
    submission = _latest_mijnbatterij_submission()
    return render_template("dashboard.html", status=status, submission=submission,
                            error=error), code


@app.route("/api/dispatch/start", methods=["POST"])
def api_dispatch_start():
    try:
        with _exclusive_action():
            result = docker_actions.start_dispatch()
    except ActionInProgress as e:
        return _dashboard_error(str(e), 409)
    if not result.ok:
        return _dashboard_error(result.stderr)
    return redirect(url_for("dashboard"))


@app.route("/api/dispatch/stop", methods=["POST"])
def api_dispatch_stop():
    try:
        with _exclusive_action():
            result = docker_actions.stop_dispatch()
    except ActionInProgress as e:
        return _dashboard_error(str(e), 409)
    if not result.ok:
        return _dashboard_error(result.stderr)
    return redirect(url_for("dashboard"))


@app.route("/backfill", methods=["GET"])
def backfill():
    return render_template("backfill.html", result=None)


@app.route("/api/backfill/<action>", methods=["POST"])
def api_backfill(action: str):
    try:
        with _exclusive_action():
            if action == "prices":
                result = backfill_actions.backfill_prices(request.form["start"],
                                                            request.form["end"])
            elif action == "pricing":
                result = backfill_actions.backfill_pricing(request.form["start"],
                                                             request.form["end"])
            elif action == "efficiency":
                result = backfill_actions.backfill_efficiency(request.form["start"],
                                                                request.form["end"])
            elif action == "mijnbatterij-monthly":
                months = [m.strip() for m in request.form["months"].split(",") if m.strip()]
                result = backfill_actions.mijnbatterij_monthly(months)
            elif action == "mijnbatterij-resubmit":
                result = backfill_actions.mijnbatterij_resubmit_now()
            else:
                return render_template("backfill.html", result=None,
                                        error=f"unknown action {action!r}"), 404
    except backfill_actions.InvalidArgument as e:
        return render_template("backfill.html", result=None, error=str(e)), 400
    except ActionInProgress as e:
        return render_template("backfill.html", result=None, error=str(e)), 409
    return render_template("backfill.html", result=result)


@app.route("/reliability")
def reliability():
    return render_template("reliability.html", tick=None, review=None)


@app.route("/api/reliability/tick", methods=["POST"])
def api_reliability_tick():
    tick = reliability_view.is_it_deciding()
    return render_template("reliability.html", tick=tick, review=None)


@app.route("/api/reliability/review-dry-run", methods=["POST"])
def api_reliability_review():
    review = reliability_view.review_dry_run()
    return render_template("reliability.html", tick=None, review=review)


@app.route("/reliability/review-dry-run.html")
def reliability_review_report():
    return send_from_directory(reliability_view.OUTPUT_DIR, "review-dry-run.html")


@app.route("/live", methods=["GET"])
def live():
    status = docker_actions.dispatch_status()
    return render_template("live.html", status=status, error=None)


def _confirmation_phrase(target_live: bool) -> str:
    return "MAKE DISPATCH LIVE" if target_live else "MAKE DISPATCH DRY-RUN"


def _poll_dispatch_status_until(target_live: bool, attempts: int = 6,
                                 interval_s: float = 5.0) -> dict:
    """Re-reads dispatch_status() up to `attempts` times, ~`interval_s` apart, stopping as
    soon as the container reports the target state.

    set_dispatch_live()'s own `docker compose` call can time out client-side (180s) while
    the daemon carries on and finishes the recreate moments later -- a single immediate
    status read right after a timeout would catch dispatch mid-recreate, read `exists:
    False` (or the pre-toggle state) as the ground truth, and audit `accepted=false`
    permanently, even though the battery ends up being driven exactly as requested seconds
    afterward. This buys ~30s of patience for that daemon-side tail before deciding the
    toggle actually failed.
    """
    status = docker_actions.dispatch_status()
    for _ in range(attempts - 1):
        if status.get("live") == target_live:
            return status
        time.sleep(interval_s)
        status = docker_actions.dispatch_status()
    return status


@app.route("/api/live", methods=["POST"])
def api_live():
    target_live = request.form.get("target") == "live"
    confirmation = request.form.get("confirmation", "")
    status = docker_actions.dispatch_status()
    # None (container missing) must not read as "dry-run" -- that would let a confirmation
    # typed against a phantom "dry-run -> live" transition through, and the compose call
    # below would then create a brand-new container from scratch rather than recreating one
    # that never existed to begin with.
    current_live = status.get("live")
    from_state = "live" if current_live else ("unknown" if current_live is None else "dry-run")
    to_state = "live" if target_live else "dry-run"

    if not status.get("exists"):
        audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state,
                                        accepted=False, reason="dispatch container not found")
        return render_template(
            "live.html", status=status,
            error="dispatch container not found -- nothing to toggle."), 409

    if not status.get("running"):
        # `--force-recreate` starts the container regardless of whether it was running --
        # an operator who stopped dispatch deliberately (mid-incident, say) and then opens
        # /live sees a DRY RUN banner (dispatch_status() only reports the mode baked into
        # the stopped container's env, not that it's stopped) and could easily confirm a
        # toggle believing dispatch stays stopped. It doesn't: the recreate brings it back
        # up, live if that's what was requested. Refuse outright and make them start it
        # from the dashboard first, where the stopped state is visible.
        audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state,
                                        accepted=False, reason="dispatch is stopped")
        return render_template(
            "live.html", status=status,
            error=("dispatch is stopped -- toggling live/dry-run would also start it back "
                   "up. Start it from the dashboard first if that's what you want.")), 409

    expected = _confirmation_phrase(target_live)
    if confirmation != expected:
        audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state,
                                        accepted=False, reason="confirmation text mismatch")
        return render_template(
            "live.html", status=status,
            error=f'Type exactly "{expected}" to confirm.'), 400

    if current_live == target_live:
        # Already in the requested state -- skip the recreate entirely rather than bounce
        # dispatch for a no-op, but still log the attempt: an operator who thinks they just
        # went live and didn't should see that in the audit trail, not a silent redirect.
        audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state,
                                        accepted=True, reason="already in the requested state")
        return redirect(url_for("live"))

    try:
        with _exclusive_action():
            # set_dispatch_live() writes the override file (makedirs/open/yaml.safe_dump)
            # BEFORE it ever runs `docker compose` -- a missing or read-only `deploy/`
            # would raise there, and an uncaught exception at this point would 500 with no
            # audit point and no stdout line, which is exactly the silent-toggle-attempt
            # gap audit.py exists to close. A confirmed go-live attempt has to show up in
            # the trail even when it fails before touching Docker.
            try:
                result = docker_actions.set_dispatch_live(target_live)
            except Exception as e:
                audit.log_dispatch_live_toggle(
                    from_state=from_state, to_state=to_state, accepted=False,
                    reason=f"exception before compose: {e}")
                return render_template(
                    "live.html", status=status,
                    error=f"Could not attempt the toggle: {e}"), 500

            # Never trust `result.ok` alone for the audit -- see _poll_dispatch_status_until.
            status_after = _poll_dispatch_status_until(target_live)
            achieved = status_after.get("live") == target_live
            audit.log_dispatch_live_toggle(
                from_state=from_state, to_state=to_state, accepted=achieved,
                reason="" if achieved else f"compose result ok={result.ok}: {result.stderr}")
    except ActionInProgress as e:
        return render_template("live.html", status=status, error=str(e)), 409

    if not achieved:
        return render_template(
            "live.html", status=status_after,
            error=f"Dispatch did not end up in the requested state: {result.stderr}"), 500
    return redirect(url_for("live"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
