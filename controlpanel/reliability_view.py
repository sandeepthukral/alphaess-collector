"""Runs the real reliability scripts, unmodified, as subprocesses.

`scripts/is-it-deciding.py` and `scripts/review-dry-run.py` are bind-mounted read-only into
this container (alongside `dispatch/`, which both import from) at the identical path they
have in the repo, so `Path(__file__).resolve().parent.parent` inside them still resolves to
a real repo root and their own sys.path insertion keeps working unmodified.

Both already accept `--token-env <VAR_NAME>` (default INFLUX_TOKEN_GRAFANA), so pointing
`--token-env INFLUX_TOKEN_CONTROLPANEL` at this container's own scoped token needs no changes
to either script.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from docker_actions import HOST_REPO_PATH

SCRIPTS_DIR = f"{HOST_REPO_PATH}/scripts"
# Redirected off the repo's working tree per the plan -- this is a read-write volume mounted
# into the container solely for the HTML this script produces, not the repo checkout itself.
OUTPUT_DIR = "/data/reliability"
REVIEW_OUT = f"{OUTPUT_DIR}/review-dry-run.html"


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _run(argv: list[str], timeout: int) -> ActionResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return ActionResult(ok=proc.returncode == 0, stdout=proc.stdout,
                             stderr=proc.stderr, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        return ActionResult(ok=False, stdout=e.stdout or "", stderr=f"timed out after {timeout}s",
                             returncode=-1)


def is_it_deciding() -> ActionResult:
    return _run(["python", f"{SCRIPTS_DIR}/is-it-deciding.py",
                 "--token-env", "INFLUX_TOKEN_CONTROLPANEL"], timeout=30)


def review_dry_run() -> ActionResult:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return _run(["python", f"{SCRIPTS_DIR}/review-dry-run.py",
                 "--token-env", "INFLUX_TOKEN_CONTROLPANEL",
                 "--out", REVIEW_OUT], timeout=120)
