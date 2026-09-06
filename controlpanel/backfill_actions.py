"""Backfill and resubmission actions, run via `docker exec` against the already-running
`collector`/`mijnbatterij` containers.

Reuses their existing credentials (ALPHAESS_APP_ID/SECRET, MIJNBATTERIJ_API_KEY) instead of
giving controlpanel its own copies -- controlpanel never sees them. Every date/month argument
is validated against a strict regex BEFORE it reaches argv; the exec target and script name
are always one of the hardcoded constants below, never built from request data.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# \Z, not `$` -- `$` matches immediately before a trailing newline too, so a value ending
# "\n" (e.g. a form field submitted with one) would pass this "strict" boundary check and
# reach argv with the newline still attached. Not an injection risk either way (argv, no
# shell), just a validator that doesn't actually enforce the shape it claims to.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
MONTH_RE = re.compile(r"^\d{4}-\d{2}\Z")


class InvalidArgument(ValueError):
    pass


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _decode(value: bytes | str | None) -> str:
    # `subprocess.TimeoutExpired.stdout` is `bytes` even with `subprocess.run(text=True)` --
    # `text=`/`universal_newlines=` only governs the successful-completion path, not what
    # lands on the exception. Left undecoded this renders as a literal "b'...'" string in
    # backfill.html instead of the partial output of a backfill that ran for up to 30
    # minutes -- exactly when an operator most needs to read it.
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run(argv: list[str], timeout: int) -> ActionResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return ActionResult(ok=proc.returncode == 0, stdout=proc.stdout,
                             stderr=proc.stderr, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        return ActionResult(ok=False, stdout=_decode(e.stdout),
                             stderr=f"timed out after {timeout}s", returncode=-1)


def _validate_date(value: str, label: str) -> str:
    if not DATE_RE.match(value):
        raise InvalidArgument(f"{label} must be YYYY-MM-DD, got {value!r}")
    return value


def _validate_month(value: str, label: str) -> str:
    if not MONTH_RE.match(value):
        raise InvalidArgument(f"{label} must be YYYY-MM, got {value!r}")
    return value


def _docker_exec(container: str, script_argv: list[str], timeout: int) -> ActionResult:
    # `timeout <n>s` runs INSIDE the container, ahead of the script, so the script is the
    # one that gets SIGTERM'd when time is up -- not just this process's local `docker exec`
    # client. Without it, killing the client on our own subprocess.run() timeout only drops
    # our end of the attach connection; the script keeps running server-side in
    # collector/mijnbatterij and keeps writing to InfluxDB, invisible to us and to the
    # operator who was just told the action "timed out". The client-side timeout below is
    # kept slightly longer, as a backstop in case the in-container `timeout` itself hangs.
    argv = ["docker", "exec", container, "timeout", f"{timeout}s", *script_argv]
    return _run(argv, timeout=timeout + 30)


def backfill_prices(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _docker_exec("collector", ["python", "prices.py", "--backfill", start, end],
                         timeout=1800)


def backfill_pricing(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _docker_exec("collector", ["python", "pricing.py", "--backfill", start, end],
                         timeout=1800)


def backfill_efficiency(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _docker_exec("collector", ["python", "efficiency.py", "--backfill", start, end],
                         timeout=1800)


def mijnbatterij_monthly(months: list[str]) -> ActionResult:
    if not months:
        raise InvalidArgument("at least one month is required")
    validated = [_validate_month(m, "month") for m in months]
    return _docker_exec("mijnbatterij",
                         ["python", "mijnbatterij.py", "--monthly", *validated], timeout=900)


def mijnbatterij_resubmit_now() -> ActionResult:
    # 180s, not 60s: `--once` queries InfluxDB and then makes up to
    # MIJNBATTERIJ_MAX_RETRIES HTTP calls to mijnbatterij.nl at
    # MIJNBATTERIJ_TIMEOUT_SECONDS (15s) each -- a 60s ceiling could SIGTERM it mid-retry
    # after the platform already accepted the submission, and the panel would report
    # "timed out" for a resubmit that in fact went through, inviting a duplicate click.
    return _docker_exec("mijnbatterij", ["python", "mijnbatterij.py", "--once"], timeout=180)
