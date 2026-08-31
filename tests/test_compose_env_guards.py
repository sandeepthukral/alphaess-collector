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

# `.env` variable -> the `-d` description of the `influx auth create` that mints it. Spelled
# out rather than derived from the variable name, because it does not follow: the pusher's
# token is described by its service, `awtrix-pusher`. A derivation that happened to work for
# three of four would just fail on whichever token is added next.
MINT_DESCRIPTION = {
    "INFLUX_TOKEN_COLLECTOR": "collector",
    "INFLUX_TOKEN_PUSHER": "awtrix-pusher",
    "INFLUX_TOKEN_GRAFANA": "grafana",
    "INFLUX_TOKEN_DISPATCH": "dispatch",
}


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


@pytest.mark.parametrize("var", [v for v in GUARDED if v.startswith("INFLUX_TOKEN_")])
def test_a_guarded_token_has_a_command_that_mints_it(var):
    """Naming the variable is not enough -- the operator still has to produce a value, and
    these are `influx auth create` invocations with bucket IDs, not something to guess. The
    first version of the dispatch documentation listed the token in the table and stopped
    there, which left the reader exactly as stuck one step further along.

    Keyed on the `-d` description, which is what `influx auth list` shows later; that makes
    the check double as "every token can be identified after the fact".
    """
    service = MINT_DESCRIPTION.get(var)
    assert service, (
        f"{var} is a new guarded token with no entry in MINT_DESCRIPTION above. Add it there "
        f"and add its `influx auth create` to DEPLOY.md, \"Scoped tokens\".")
    assert re.search(rf'-d "{re.escape(service)}: ', DEPLOY), (
        f"DEPLOY.md has no `influx auth create ... -d \"{service}: ...\"` for {var}")


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


# `INFLUX_TOKEN_MIJNBATTERIJ` is the one Influx token deliberately NOT `:?`-guarded, so the
# checks above do not cover it. These two do instead: it must stay unguarded, and it must
# still be mintable.

def test_the_mijnbatterij_token_is_not_stack_wide_guarded():
    """Every `:?` guard is stack-wide -- see this module's docstring. Paid willingly for the
    services the stack runs; wrong for `mijnbatterij`, which is opt-in and idles without
    MIJNBATTERIJ_API_KEY. Guarding it would break `sudo docker compose ps` on the NAS the
    moment this branch is pulled, for a feature nobody had switched on. Nothing is lost:
    the guards exist so a missing token cannot silently become the ADMIN token, and an empty
    value is not the admin token -- it fails to authenticate. mijnbatterij.py refuses to
    start on an empty token when an API key is set, which puts the error in the service it
    concerns."""
    assert "INFLUX_TOKEN_MIJNBATTERIJ" not in GUARDED
    assert "${INFLUX_TOKEN_MIJNBATTERIJ:-}" in COMPOSE_TEXT


def test_the_mijnbatterij_token_still_has_a_command_that_mints_it():
    """Being unguarded makes it easier to forget, not less necessary."""
    assert re.search(r'-d "mijnbatterij: ', DEPLOY)
