"""controlpanel's compose invocation needs its OWN env file (deploy/controlpanel.env), never
the real .env -- see deploy/controlpanel.env.example for why. That file is hand-maintained,
which means it silently rots the day someone adds a variable to docker-compose.yml and
forgets it. These tests are the guard: every variable the `dispatch` service actually
consumes, and every OTHER service's `:?`-guarded variable, must have a matching key in the
example file, or the live toggle breaks (or worse, silently recreates dispatch with a
default instead of the operator's real value) the next time someone uses it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
ENV_EXAMPLE_TEXT = (REPO / "deploy" / "controlpanel.env.example").read_text(encoding="utf-8")

VAR_REF = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)')
REQUIRED_REF = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*):\?')


def _refs(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, str):
        found.update(VAR_REF.findall(node))
    elif isinstance(node, dict):
        for v in node.values():
            found |= _refs(v)
    elif isinstance(node, list):
        for v in node:
            found |= _refs(v)
    return found


def _example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE_TEXT.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0])
    return keys


def test_every_dispatch_variable_has_a_real_entry():
    """These values become the RECREATED dispatch container's actual config -- a missing one
    here does not fail loudly, it silently ships a default the next time the live toggle
    runs `--force-recreate dispatch`."""
    dispatch_vars = _refs(COMPOSE["services"]["dispatch"])
    example_keys = _example_keys()
    missing = dispatch_vars - example_keys
    assert not missing, (
        f"deploy/controlpanel.env.example is missing dispatch variable(s) {sorted(missing)} "
        f"-- add real values, not placeholders (see the file's own comment)")


def test_every_required_variable_across_the_stack_is_satisfied():
    """`docker compose` interpolates the WHOLE file before touching only `dispatch`, so every
    `:?`-guarded variable anywhere must resolve or the live toggle's compose call aborts
    before it ever reaches dispatch."""
    all_required: set[str] = set()
    for line in (REPO / "docker-compose.yml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        all_required.update(REQUIRED_REF.findall(line))
    example_keys = _example_keys()
    missing = all_required - example_keys
    assert not missing, (
        f"deploy/controlpanel.env.example does not satisfy required variable(s) "
        f"{sorted(missing)} -- add a placeholder (see the file's own comment)")


def test_the_example_never_carries_a_secret_from_another_service():
    """The whole point of a separate file: controlpanel must never be handed a real
    ALPHAESS/mijnbatterij/collector credential just to make a compose call parse. Checked
    against the parsed keys, not the raw text, since the file's own comments discuss these
    names on purpose (to explain why they are absent)."""
    forbidden = ("ALPHAESS_APP_SECRET", "ALPHAESS_APP_ID", "MIJNBATTERIJ_API_KEY")
    example_keys = _example_keys()
    for name in forbidden:
        assert name not in example_keys, (
            f"{name} has no business as a key in controlpanel's env file")


def test_the_override_file_is_gitignored_and_never_the_auto_loaded_name():
    """`docker-compose.override.yml` is auto-loaded by `docker compose` on every bare command
    an operator runs by hand on the NAS. The live-toggle override must never be named that,
    and must never be tracked -- it is rewritten by controlpanel on every toggle."""
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/dispatch-live.override.yml" in gitignore
    assert "deploy/controlpanel.env" in gitignore
    assert "docker-compose.override.yml" not in gitignore, (
        "that name is auto-loaded by bare `docker compose` commands -- the live-toggle file "
        "must use a different name entirely, not merely be gitignored under this one")
