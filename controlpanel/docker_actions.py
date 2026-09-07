"""Every Docker- and compose-touching action controlpanel can take.

Every function here ends in exactly one `subprocess.run([...])` call with a fixed argv list
built from constants and, at most, a validated boolean/enum -- never from unvalidated request
data reaching argv or a shell. That is the tradeoff for mounting the Docker socket into this
container: this file is the entire allowlist, and it is meant to be read end to end.
"""
from __future__ import annotations

import json
import os

import yaml
from subprocess_utils import ActionResult
from subprocess_utils import run as _run

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

class EnvUnconfigured(Exception):
    """Raised by set_dispatch_live(True) when deploy/controlpanel.env fails a sanity check --
    distinct from a compose failure so callers can skip the post-toggle status poll (there
    is nothing to poll for: nothing was ever run) and report the real reason directly."""


def _parse_env_file(text: str) -> dict[str, str]:
    """`KEY=value` lines -> a dict, tolerant of the ways a hand-edited file actually varies:
    surrounding whitespace (a stray trailing space defeated the exact-match placeholder
    check below), and a value wrapped in quotes (some editors/shells add them on paste;
    `"real-token"` must compare equal to `real-token`, not to itself literally)."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _parse_docker_env_list(env_lines: list[str]) -> dict[str, str]:
    """`docker inspect`'s `Config.Env`, a list of literal `KEY=VALUE` strings -- same
    tolerant parsing as `_parse_env_file` (Docker doesn't quote these, but nothing here
    depends on that not changing)."""
    return _parse_env_file("\n".join(env_lines))


# The literal values deploy/controlpanel.env.example ships for two "REAL values" keys that
# can never legitimately match the example verbatim on a real install -- a per-install
# InfluxDB token and a real device serial number. tests/test_controlpanel_env_completeness.py
# only checks that a key is PRESENT in deploy/controlpanel.env, never that its value was
# actually edited away from the example; DEPLOY.md step 7 tells the operator to copy real
# values in by hand, but nothing stopped `cp deploy/controlpanel.env.example
# deploy/controlpanel.env` alone from passing every existing check. A match on either of
# these means step 7 never happened, and --force-recreate would then bring dispatch up
# LIVE against the wrong inverter identity/token -- checked in set_dispatch_live() below,
# only when going live, since dry-run stays safe regardless.
_UNCONFIGURED_MARKERS = {
    "ALPHAESS_SYS_SN": "your_system_serial_number",
    "INFLUX_TOKEN_DISPATCH": "read_planning_read_write_alphaess",
}

# The seven Kuma push URLs dispatch's `docker-compose.yml` block declares. Unlike the two
# markers above, blank is a LEGITIMATE value here in general (unset = no heartbeat, the
# same `:-` default the compose file itself falls back to) -- so these can't be guarded by
# "must not equal a placeholder." What they can be guarded against is REGRESSING: if the
# container currently running already has one of these populated (a real .env with real
# Kuma URLs, already live), a go-live recreate must never silently blank it out just
# because deploy/controlpanel.env was copied without filling this section in. See
# DISPATCH-GOLIVE.md section 3 -- these back the dead-man's-switch pushes required before
# `--live`, and dispatch has no way to notice its own heartbeat went dark.
_HEARTBEAT_URL_KEYS = (
    "PLAN_INFLUX_HEARTBEAT_URL",
    "SLOTS_WRITTEN_HEARTBEAT_URL",
    "SLOTS_FRESH_HEARTBEAT_URL",
    "DISPATCHER_ALIVE_HEARTBEAT_URL",
    "DISPATCH_CONFIRMED_HEARTBEAT_URL",
    "INVERTER_NOT_HIJACKED_HEARTBEAT_URL",
    "SOC_FLOOR_HEARTBEAT_URL",
)


def _controlpanel_env_unconfigured_reason() -> str | None:
    try:
        with open(CONTROLPANEL_ENV_FILE, encoding="utf-8") as f:
            values = _parse_env_file(f.read())
    except OSError as e:
        return f"could not read {CONTROLPANEL_ENV_FILE}: {e}"
    for key, placeholder in _UNCONFIGURED_MARKERS.items():
        if values.get(key) == placeholder:
            return (f"{key} in deploy/controlpanel.env still holds the example's shipped "
                     f"placeholder value -- see DEPLOY.md, \"Control panel\" step 7")
    return None


def _heartbeat_regression_reason() -> str | None:
    proc = _run(["docker", "inspect", DISPATCH_CONTAINER], timeout=60)
    if not proc.ok:
        return None  # nothing running yet to regress FROM -- dispatch_status() etc. handle
                     # "container doesn't exist" elsewhere; this check has nothing to add.
    try:
        current = _parse_docker_env_list(json.loads(proc.stdout)[0]["Config"]["Env"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None  # same reasoning as dispatch_status()'s own parse guard

    try:
        with open(CONTROLPANEL_ENV_FILE, encoding="utf-8") as f:
            new = _parse_env_file(f.read())
    except OSError as e:
        return f"could not read {CONTROLPANEL_ENV_FILE}: {e}"

    regressing = sorted(
        key for key in _HEARTBEAT_URL_KEYS
        if current.get(key, "").strip() and not new.get(key, "").strip()
    )
    if regressing:
        return (f"deploy/controlpanel.env would blank out already-configured heartbeat "
                 f"URL(s) {regressing} on the recreated dispatch container -- copy them "
                 f"from the real .env first (see DEPLOY.md, \"Control panel\" step 7)")
    return None


def dispatch_status() -> dict:
    """Current running state and DISPATCH_LIVE value, read from the container itself --
    never from the override file, which could be stale or never applied."""
    proc = _run(["docker", "inspect", DISPATCH_CONTAINER], timeout=60)
    if not proc.ok:
        return {"exists": False, "running": False, "live": None,
                "not_found": True, "error": proc.stderr}

    # `dispatch_status()` backs the dashboard's Start/Stop buttons and the live-toggle
    # banner -- both need to render even if `docker inspect`'s own output can't be parsed
    # (an unexpected daemon/CLI version skew, truncated output). Without this, a bad parse
    # here 500s the whole page instead of just leaving the mode/state unknown, which is
    # exactly the state operators most need those buttons to survive.
    try:
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
    except (ValueError, KeyError, IndexError, TypeError) as e:
        # `exists: False` here, even though `docker inspect` itself succeeded -- the
        # dashboard and live-toggle templates already treat that as "state unknown, don't
        # claim dry-run/stopped" (see live.html's amber banner), which is exactly right when
        # this container's actual state genuinely can't be determined from what came back.
        # `not_found` stays absent (falsy): the container plainly exists, `docker inspect`
        # said so -- api_live() uses this to tell "nothing to toggle" apart from "state
        # unknown because the output couldn't be parsed", which need a different message and
        # a different operator response.
        return {"exists": False, "running": False, "live": None,
                "error": f"could not parse `docker inspect` output: {e}"}


def start_dispatch() -> ActionResult:
    return _run(["docker", "start", DISPATCH_CONTAINER], timeout=60)


def stop_dispatch() -> ActionResult:
    # Grace period is the service's own `stop_grace_period: 30s` in docker-compose.yml --
    # `docker stop` already honours it without a flag here.
    return _run(["docker", "stop", DISPATCH_CONTAINER], timeout=45)


def set_dispatch_live(live: bool) -> ActionResult:
    """The one action that uses `docker compose` rather than bare `docker`, per
    docs/DEPLOY.md, "The DISPATCH_LIVE mechanism". Writes the override file, then recreates
    only the dispatch service against it."""
    if live:
        reason = _controlpanel_env_unconfigured_reason() or _heartbeat_regression_reason()
        if reason:
            raise EnvUnconfigured(reason)

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
        # --no-deps: dispatch declares `depends_on: influxdb`, and without this flag
        # `--force-recreate` also recreates influxdb -- bouncing every other service that
        # depends on it (collector, grafana, awtrix-pusher, mijnbatterij) just to toggle a
        # dispatch env var, and doing so against controlpanel.env's placeholder InfluxDB
        # credentials rather than the real ones in .env.
        "up", "-d", "--force-recreate", "--no-deps", DISPATCH_CONTAINER,
    # dispatch's `stop_grace_period: 30s` alone can absorb most of a short timeout before
    # the new container even starts; give real headroom over the worst case rather than
    # timing out on a recreate that was actually still in progress. api_live() re-checks
    # the container's actual state afterwards regardless of what this returns, precisely
    # because a client-side timeout here does not mean the daemon-side recreate stopped.
    ], timeout=180)
