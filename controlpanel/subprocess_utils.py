"""Shared subprocess wrapper for controlpanel's docker/script-invoking modules.

`docker_actions.py`, `backfill_actions.py`, and `reliability_view.py` each shell out via
`subprocess.run` and need the identical result shape and the identical
`TimeoutExpired.stdout`-is-`bytes` workaround (see `_decode()`) -- kept in one place instead
of three copy-pasted ones that would drift the moment one gets a fix the others don't.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class ActionResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _decode(value: bytes | str | None) -> str:
    # `subprocess.TimeoutExpired.stdout` is `bytes` even with `subprocess.run(text=True)` --
    # `text=`/`universal_newlines=` only governs the successful-completion path, not what
    # lands on the exception. Left undecoded this renders as a literal "b'...'" string
    # instead of the actual partial output -- exactly when an operator most needs to read it.
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run(argv: list[str], timeout: int) -> ActionResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return ActionResult(ok=proc.returncode == 0, stdout=proc.stdout,
                             stderr=proc.stderr, returncode=proc.returncode)
    except subprocess.TimeoutExpired as e:
        return ActionResult(ok=False, stdout=_decode(e.stdout),
                             stderr=f"timed out after {timeout}s", returncode=-1)
