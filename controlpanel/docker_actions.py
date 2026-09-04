"""Every Docker- and compose-touching action controlpanel can take.

Every function here ends in exactly one `subprocess.run([...])` call with a fixed argv list
built from constants and, at most, a validated boolean/enum -- never from unvalidated request
data reaching argv or a shell. That is the tradeoff for mounting the Docker socket into this
container: this file is the entire allowlist, and it is meant to be read end to end.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

import yaml

# The identical absolute path this repo lives at on the host (e.g.
# /volume1/docker/alphaess-collector). Required -- see DEPLOY.md, "Control panel" -- because
# `docker compose` (the client, running in here) resolves relative paths in the compose file
# against --project-directory before sending them to the daemon, and the daemon can only
# mount paths that exist on the HOST. Verified by hand on the real NAS; see that section for
# the reasoning this constant exists at all.
HOST_REPO_PATH = os.environ["HOST_REPO_PATH"]
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "alphaess-collector")

COMPOSE_FILE = f"{HOST_REPO_PATH}/docker-compose.yml"
# Deliberately NOT docker-compose.override.yml, which `docker compose` auto-loads on every
# bare command run by hand on the NAS. This file is only ever read via the explicit -f flag
# in the subprocess call below, so it can never silently change behaviour for a command an
# operator runs themselves.
OVERRIDE_FILE = f"{HOST_REPO_PATH}/deploy/dispatch-live.override.yml"
# controlpanel's OWN env file for compose variable interpolation -- real dispatch-scoped
# values plus harmless placeholders for every other service's required variable. NEVER the
# real .env: that file holds ALPHAESS_APP_SECRET, MIJNBATTERIJ_API_KEY and every other
# service's InfluxDB token, none of which this container is meant to be able to read. See
# deploy/controlpanel.env.example and tests/test_controlpanel_env_completeness.py, which
# fails the build the day a new required variable is added anywhere without a placeholder
# here.
CONTROLPANEL_ENV_FILE = f"{HOST_REPO_PATH}/deploy/controlpanel.env"

DISPATCH_CONTAINER = "dispatch"
COLLECTOR_CONTAINER = "collector"
MIJNBATTERIJ_CONTAINER = "mijnbatterij"


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _run(argv: list[str], timeout: int = 60) -> ActionResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return ActionResult(ok=proc.returncode == 0, stdout=proc.stdout,
                             stderr=proc.stderr, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        return ActionResult(ok=False, stdout=e.stdout or "", stderr=f"timed out after {timeout}s",
                             returncode=-1)


def dispatch_status() -> dict:
    """Current running state and DISPATCH_LIVE value, read from the container itself --
    never from the override file, which could be stale or never applied."""
    proc = _run(["docker", "inspect", DISPATCH_CONTAINER])
    if not proc.ok:
        return {"exists": False, "running": False, "live": None, "error": proc.stderr}

    info = json.loads(proc.stdout)[0]
    env_lines = info["Config"]["Env"]
    live_raw = next((ln.split("=", 1)[1] for ln in env_lines
                      if ln.startswith("DISPATCH_LIVE=")), "0")
    live = live_raw.strip().lower() in ("1", "true", "yes", "on")
    return {
        "exists": True,
        "running": info["State"]["Running"],
        "started_at": info["State"].get("StartedAt"),
        "live": live,
        "live_raw": live_raw,
    }


def start_dispatch() -> ActionResult:
    return _run(["docker", "start", DISPATCH_CONTAINER])


def stop_dispatch() -> ActionResult:
    # Grace period is the service's own `stop_grace_period: 30s` in docker-compose.yml --
    # `docker stop` already honours it without a flag here.
    return _run(["docker", "stop", DISPATCH_CONTAINER], timeout=45)


def set_dispatch_live(live: bool) -> ActionResult:
    """The one action that uses `docker compose` rather than bare `docker`, per
    docs/DEPLOY.md, "The DISPATCH_LIVE mechanism". Writes the override file, then recreates
    only the dispatch service against it."""
    override = {
        "services": {
            "dispatch": {
                "environment": {
                    "DISPATCH_LIVE": "1" if live else "0",
                }
            }
        }
    }
    os.makedirs(os.path.dirname(OVERRIDE_FILE), exist_ok=True)
    with open(OVERRIDE_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(override, f)

    return _run([
        "docker", "compose",
        "-f", COMPOSE_FILE,
        "-f", OVERRIDE_FILE,
        "--env-file", CONTROLPANEL_ENV_FILE,
        "--project-directory", HOST_REPO_PATH,
        "-p", COMPOSE_PROJECT_NAME,
        "up", "-d", "--force-recreate", DISPATCH_CONTAINER,
    ], timeout=120)
