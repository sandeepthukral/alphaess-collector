"""Web control panel for the dispatcher and other console-only ops.

A friendlier front door to commands that already exist and already work -- not new dispatch
logic. Reachable on the LAN behind nginx basic auth (see nginx/controlpanel.conf); this app
itself is never published directly. See DEPLOY.md, "Control panel".
"""
from __future__ import annotations

import os

import audit
import backfill_actions
import docker_actions
import reliability_view
from flask import Flask, redirect, render_template, request, send_from_directory, url_for
from influxdb_client import InfluxDBClient

app = Flask(__name__)

INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "alphaess")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN_CONTROLPANEL"]

_influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_query_api = _influx.query_api()


def _latest_mijnbatterij_submission() -> dict | None:
    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -26h)
      |> filter(fn: (r) => r._measurement == "mijnbatterij_submit")
      |> filter(fn: (r) => r._field == "submitted" or r._field == "status_code")
      |> last()
    '''
    tables = _query_api.query(flux)
    result: dict = {}
    for table in tables:
        for record in table.records:
            result["time"] = record.get_time()
            result["outcome"] = record.values.get("outcome")
            result[record.get_field()] = record.get_value()
    return result or None


@app.route("/")
def dashboard():
    status = docker_actions.dispatch_status()
    tick = reliability_view.is_it_deciding()
    submission = _latest_mijnbatterij_submission()
    return render_template("dashboard.html", status=status, tick=tick, submission=submission)


@app.route("/api/dispatch/start", methods=["POST"])
def api_dispatch_start():
    docker_actions.start_dispatch()
    return redirect(url_for("dashboard"))


@app.route("/api/dispatch/stop", methods=["POST"])
def api_dispatch_stop():
    docker_actions.stop_dispatch()
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
    from_state = "live" if status.get("live") else "dry-run"
    to_state = "live" if target_live else "dry-run"

    expected = _confirmation_phrase(target_live)
    if confirmation != expected:
        audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state,
                                        accepted=False, reason="confirmation text mismatch")
        return render_template(
            "live.html", status=status,
            error=f'Type exactly "{expected}" to confirm.'), 400

    docker_actions.set_dispatch_live(target_live)
    audit.log_dispatch_live_toggle(from_state=from_state, to_state=to_state, accepted=True)
    return redirect(url_for("live"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
