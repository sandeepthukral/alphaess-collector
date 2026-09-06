"""Web control panel for the dispatcher and other console-only ops.

A friendlier front door to commands that already exist and already work -- not new dispatch
logic. Reachable on the LAN behind nginx basic auth (see nginx/controlpanel.conf); this app
itself is never published directly. See DEPLOY.md, "Control panel".
"""
from __future__ import annotations

import os
import secrets

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
    # `last()` alone picks the latest row WITHIN each of those tables, and the loop below
    # then just overwrites the result dict in whatever order Influx returns the tables --
    # an old "error" table iterated after a newer "ok" one would win. `pivot` first merges
    # the two fields of the same point into one row (keyed on time), `group()` collapses
    # every outcome's table into one, and `sort`+`limit` then picks the single globally
    # latest row across all of them.
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -26h)
      |> filter(fn: (r) => r._measurement == "mijnbatterij_submit")
      |> filter(fn: (r) => r._field == "submitted" or r._field == "status_code")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> group()
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    # Influx is not on the critical path for start/stop -- those act on the dispatch
    # container directly, over the Docker socket, with no Influx involved. Losing this
    # widget must never take the whole dashboard (and its start/stop buttons) down with it,
    # which is exactly when an operator needs them most.
    try:
        tables = _query_api.query(flux)
    except Exception:
        return None
    for table in tables:
        for record in table.records:
            return {
                "time": record.get_time(),
                "outcome": record.values.get("outcome"),
                "submitted": record.values.get("submitted"),
                "status_code": record.values.get("status_code"),
            }
    return None


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


@app.route("/api/dispatch/start", methods=["POST"])
def api_dispatch_start():
    result = docker_actions.start_dispatch()
    if not result.ok:
        status = docker_actions.dispatch_status()
        submission = _latest_mijnbatterij_submission()
        return render_template("dashboard.html", status=status,
                                submission=submission, error=result.stderr), 500
    return redirect(url_for("dashboard"))


@app.route("/api/dispatch/stop", methods=["POST"])
def api_dispatch_stop():
    result = docker_actions.stop_dispatch()
    if not result.ok:
        status = docker_actions.dispatch_status()
        submission = _latest_mijnbatterij_submission()
        return render_template("dashboard.html", status=status,
                                submission=submission, error=result.stderr), 500
    return redirect(url_for("dashboard"))


@app.route("/backfill", methods=["GET"])
def backfill():
    return render_template("backfill.html", result=None)


@app.route("/api/backfill/<action>", methods=["POST"])
def api_backfill(action: str):
    try:
        if action == "prices":
            result = backfill_actions.backfill_prices(request.form["start"], request.form["end"])
        elif action == "pricing":
            result = backfill_actions.backfill_pricing(request.form["start"], request.form["end"])
        elif action == "efficiency":
            result = backfill_actions.backfill_efficiency(request.form["start"], request.form["end"])
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

    result = docker_actions.set_dispatch_live(target_live)
    # Never trust `result.ok` alone for the audit: a client-side timeout here (see
    # docker_actions.set_dispatch_live) kills our `docker compose` CLI, not the recreate the
    # daemon is carrying out -- that can go on to succeed after we already reported failure.
    # Reading the container back after the call is the only way to know what actually
    # happened, and is exactly what dashboard()/live() themselves trust.
    status_after = docker_actions.dispatch_status()
    achieved = status_after.get("live") == target_live
    audit.log_dispatch_live_toggle(
        from_state=from_state, to_state=to_state, accepted=achieved,
        reason="" if achieved else f"compose result ok={result.ok}: {result.stderr}")
    if not achieved:
        return render_template(
            "live.html", status=status_after,
            error=f"Dispatch did not end up in the requested state: {result.stderr}"), 500
    return redirect(url_for("live"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
