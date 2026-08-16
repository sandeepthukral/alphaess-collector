"""The dispatch service's deployment shape. DESIGN-dispatch.md section 10.

These read `docker-compose.yml`, the Dockerfile and the entrypoint as text rather than booting
anything. That is the point: every property here is one an edit can quietly undo, and each of
them costs real money or real hardware risk the first time nobody notices.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
DISPATCH = COMPOSE["services"]["dispatch"]
DOCKERFILE = (REPO / "dispatch" / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (REPO / "dispatch" / "entrypoint.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")


def test_the_service_starts_in_dry_run():
    """Going live is an operator action taken while watching the dashboard, with the AlphaESS
    app's own price control already off. A `--live` that arrives by merge instead is two
    controllers fighting over the same registers on somebody else's schedule."""
    assert "--live" not in DISPATCH["command"], (
        "dispatch would start writing to the inverter on deploy -- see the plan's "
        "verification steps 6 and 7")


def test_there_is_exactly_one_dispatch_service_and_it_does_not_scale():
    """The inverter accepts ONE Modbus TCP connection. A second replica does not degrade, it
    takes the connection away from the first."""
    modbus = [name for name, svc in COMPOSE["services"].items()
              if "dispatch" in str(svc.get("build", ""))]
    assert modbus == ["dispatch"]
    assert "deploy" not in DISPATCH, "no replica count on the service holding :502"
    assert "scale" not in DISPATCH


def test_slots_live_on_a_volume_not_in_the_image():
    """A rebuild must not discard the schedule the battery is currently following."""
    assert "alphaess-dispatch-data:/data" in DISPATCH["volumes"]
    assert "alphaess-dispatch-data" in COMPOSE["volumes"]


def test_the_service_restarts_and_rotates_its_logs():
    assert DISPATCH["restart"] == "unless-stopped"
    assert DISPATCH["logging"]["options"]["max-size"], "unbounded logs on the NAS"


def test_it_reads_the_planning_bucket_with_its_own_token():
    """A token spanning both buckets exists only for this service; sharing the collector's
    would widen that one instead."""
    env = DISPATCH["environment"]
    assert "INFLUX_TOKEN_DISPATCH" in env["INFLUX_TOKEN"]
    assert env["PLANNING_BUCKET"].startswith("${PLANNING_BUCKET")
    assert "INFLUX_TOKEN_DISPATCH" in ENV_EXAMPLE


def test_the_token_has_no_fallback():
    """`:-` would let a missing token fall back to something plausible. `:?` fails the deploy
    instead, which is the lesson of the scoped-token migration."""
    assert "${INFLUX_TOKEN_DISPATCH:?" in (REPO / "docker-compose.yml").read_text()


@pytest.mark.parametrize("module", ["corpus.py", "test_mode1_negative.py"])
def test_the_image_excludes_what_must_not_run_in_it(module):
    """`corpus.py` reads archived household plan data; `test_mode1_negative.py` opens its own
    Modbus connection, which would steal the dispatcher's."""
    assert f"COPY {module}" not in DOCKERFILE


@pytest.mark.parametrize("module", [
    "registers.py", "heartbeat.py", "plan.py", "translator.py", "translate.py",
    "slots.py", "state.py", "scheduler.py"])
def test_the_image_carries_every_module_it_imports(module):
    assert f"COPY {module}" in DOCKERFILE


def test_the_healthcheck_never_opens_a_modbus_connection():
    """Section 6.2: any inverter-facing check must be self-reported. `--alive` reads the
    heartbeat file only."""
    assert "--alive" in DOCKERFILE
    assert "--ip" not in DOCKERFILE


def test_the_scheduler_is_the_process_that_receives_sigterm():
    """`release on exit` only runs if SIGTERM reaches the scheduler. Without the exec, the
    shell is PID 1 and `docker compose stop` leaves a command live until its dead man's switch
    expires."""
    assert "exec python -u /app/scheduler.py" in ENTRYPOINT


def test_the_container_user_has_a_pinned_uid():
    """Docker seeds a named volume's ownership from the image only when it CREATES the volume.
    `alphaess-dispatch-data` already exists on the NAS, chowned to the UID `useradd` happened
    to pick. An implicit UID means a later edit adding a user above it shifts this one, and the
    rebuilt container cannot write slots.json to its own volume."""
    assert "--uid 1000" in DOCKERFILE


def test_the_refresh_interval_is_validated_before_it_reaches_sleep():
    """Under `set -e` a bad TRANSLATE_INTERVAL_S kills the background loop but NOT the
    container: the scheduler keeps dispatching as PID 1 against a slots.json that silently
    stops refreshing. One typo in a compose variable is enough to get there."""
    assert "TRANSLATE_INTERVAL_S" in ENTRYPOINT
    assert "*[!0-9]*" in ENTRYPOINT, "the interval reaches `sleep` unvalidated"
    assert "sleep \"$INTERVAL\" ||" not in ENTRYPOINT, (
        "`|| true` keeps the loop alive by removing the delay -- it would re-query InfluxDB "
        "as fast as the NAS can answer")


def test_a_failed_first_translation_does_not_stop_the_container():
    """A stale slots.json is a monitored, gracefully-degrading state. A container that refuses
    to start because InfluxDB blinked is a worse failure than the one it avoids."""
    assert "set -eu" in ENTRYPOINT, "the entrypoint should still fail on unset variables"
    line = next(line for line in ENTRYPOINT.splitlines()
                if line.startswith("python -u /app/translate.py"))
    assert line.rstrip().endswith("|| \\"), "the initial translation must be non-fatal"
