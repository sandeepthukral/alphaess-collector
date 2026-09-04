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

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class InvalidArgument(ValueError):
    pass


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


def _validate_date(value: str, label: str) -> str:
    if not DATE_RE.match(value):
        raise InvalidArgument(f"{label} must be YYYY-MM-DD, got {value!r}")
    return value


def _validate_month(value: str, label: str) -> str:
    if not MONTH_RE.match(value):
        raise InvalidArgument(f"{label} must be YYYY-MM, got {value!r}")
    return value


def backfill_prices(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _run(["docker", "exec", "collector", "python", "prices.py",
                 "--backfill", start, end], timeout=1800)


def backfill_pricing(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _run(["docker", "exec", "collector", "python", "pricing.py",
                 "--backfill", start, end], timeout=1800)


def backfill_efficiency(start: str, end: str) -> ActionResult:
    start, end = _validate_date(start, "start"), _validate_date(end, "end")
    return _run(["docker", "exec", "collector", "python", "efficiency.py",
                 "--backfill", start, end], timeout=1800)


def mijnbatterij_monthly(months: list[str]) -> ActionResult:
    if not months:
        raise InvalidArgument("at least one month is required")
    validated = [_validate_month(m, "month") for m in months]
    return _run(["docker", "exec", "mijnbatterij", "python", "mijnbatterij.py",
                 "--monthly", *validated], timeout=900)


def mijnbatterij_resubmit_now() -> ActionResult:
    return _run(["docker", "exec", "mijnbatterij", "python", "mijnbatterij.py",
                 "--once"], timeout=60)
