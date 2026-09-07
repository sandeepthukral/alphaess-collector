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

from docker_actions import HOST_REPO_PATH
from subprocess_utils import ActionResult
from subprocess_utils import run as _run

SCRIPTS_DIR = f"{HOST_REPO_PATH}/scripts"
# Redirected off the repo's working tree per the plan -- this is a read-write volume mounted
# into the container solely for the HTML this script produces, not the repo checkout itself.
OUTPUT_DIR = "/data/reliability"
REVIEW_OUT = f"{OUTPUT_DIR}/review-dry-run.html"


INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "alphaess")


def is_it_deciding() -> ActionResult:
    return _run(["python", f"{SCRIPTS_DIR}/is-it-deciding.py",
                 "--bucket", INFLUX_BUCKET,
                 "--token-env", "INFLUX_TOKEN_CONTROLPANEL"], timeout=30)


def review_dry_run() -> ActionResult:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return _run(["python", f"{SCRIPTS_DIR}/review-dry-run.py",
                 "--bucket", INFLUX_BUCKET,
                 "--token-env", "INFLUX_TOKEN_CONTROLPANEL",
                 "--out", REVIEW_OUT], timeout=120)
