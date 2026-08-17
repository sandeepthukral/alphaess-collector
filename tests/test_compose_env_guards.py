"""Every `:?`-guarded variable in docker-compose.yml is documented in DEPLOY.md.

These guards are stack-wide, not per-service. Compose interpolates the whole file on every
subcommand, so one missing key stops `docker compose restart grafana` -- a command touching
neither the guarded service nor Influx. That is the guard working as designed, and DEPLOY.md
says so. It only reads as a broken checkout when the variable it names is not written down
anywhere, which is what happened with INFLUX_TOKEN_DISPATCH on 2026-08-17: the service landed
before go-live, the error pointed at DEPLOY.md's "Scoped tokens" section, and that section did
not mention it.

CI cannot catch the underlying problem by booting: ci.yml copies .env.example, which carries a
placeholder for every key, so a new guard is green there and blocking on the NAS. The check
that generalises is not "does it boot" but "can the operator find out what to set".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
COMPOSE_TEXT = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
DEPLOY = (REPO / "DEPLOY.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")

GUARDED = sorted(set(re.findall(r"\$\{([A-Z0-9_]+):\?", COMPOSE_TEXT)))


def test_the_guards_were_actually_found():
    """A regex that silently matches nothing would make every test below vacuously pass."""
    assert len(GUARDED) >= 4, GUARDED


@pytest.mark.parametrize("var", GUARDED)
def test_a_guarded_variable_is_documented_in_deploy_md(var):
    assert var in DEPLOY, (
        f"{var} has a `:?` guard, so a NAS without it cannot run ANY compose subcommand, "
        f"but DEPLOY.md never names it. The operator gets a variable name, follows the "
        f"pointer in the error, and finds nothing there.")


@pytest.mark.parametrize("var", GUARDED)
def test_a_guarded_variable_has_a_placeholder_in_env_example(var):
    """Also what keeps CI green, which is why it cannot be the only check."""
    assert re.search(rf"^{var}=", ENV_EXAMPLE, re.M), ENV_EXAMPLE[:0] or var


@pytest.mark.parametrize("var", GUARDED)
def test_the_error_message_points_somewhere_that_exists(var):
    """Each guard names a DEPLOY.md section. A pointer to a section that was renamed is worse
    than no pointer: it reads as authoritative and sends the reader to the wrong page."""
    for message in re.findall(rf"\$\{{{var}:\?([^}}]*)\}}", COMPOSE_TEXT):
        for section in re.findall(r'DEPLOY\.md, "([^"]+)"', message):
            # `\d+\.` because the first four headings are numbered steps and the error
            # messages cite them by name alone, which is how a reader looks them up.
            assert re.search(rf"^#+ (?:\d+\. )?{re.escape(section)}\s*$", DEPLOY, re.M | re.I), (
                f"{var}'s error sends the reader to DEPLOY.md section {section!r}, "
                f"which has no matching heading")
