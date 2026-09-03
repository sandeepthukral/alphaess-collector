"""Every service that pings Uptime Kuma can resolve the name its URLs use.

The ten `*_HEARTBEAT_URL` settings name the host `kuma`, which no resolver knows: it is an
`extra_hosts` alias written into the container's /etc/hosts by docker-compose.yml. That
indirection exists because the literal IP it replaced moved twice in five days (2026-08-29,
2026-09-02) and took every heartbeat with it, silently -- heartbeat failures are logged and
swallowed by design, so the only symptom is a monitor that reports the *monitored thing* as
down while it is in fact healthy.

The failure this file guards is the one that indirection introduces: a service that gains a
heartbeat URL but not the alias. It cannot be caught by booting, because an unresolvable host
is a warning in a log and nothing more, and it cannot be caught in CI by pinging, because
there is no Kuma there. What is checkable is that the two halves agree in the compose file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")
DEPLOY = (REPO / "DEPLOY.md").read_text(encoding="utf-8")

ALIAS = "kuma"
EXPECTED_ENTRY = "kuma:${KUMA_ADDR:-host-gateway}"

SERVICES = COMPOSE["services"]
PINGERS = sorted(
    name
    for name, svc in SERVICES.items()
    if any(k.endswith("HEARTBEAT_URL") for k in (svc.get("environment") or {}))
)


def test_the_pinging_services_were_actually_found():
    """A shape change in the compose file would make every test below vacuously pass."""
    assert PINGERS == ["collector", "dispatch", "mijnbatterij"], PINGERS


def test_all_ten_heartbeat_urls_are_accounted_for():
    """The count is the thing TODO 18 is measured against: two collector-side (live and
    nightly efficiency), one mijnbatterij, seven dispatch. A new monitor should trip this and
    be added deliberately, not slip in unnoticed."""
    urls = [
        key
        for svc in SERVICES.values()
        for key in (svc.get("environment") or {})
        if key.endswith("HEARTBEAT_URL")
    ]
    assert len(urls) == 10, sorted(urls)


@pytest.mark.parametrize("service", PINGERS)
def test_a_pinging_service_declares_the_kuma_alias(service):
    entries = SERVICES[service].get("extra_hosts") or []
    assert EXPECTED_ENTRY in entries, (
        f"{service} sets a *_HEARTBEAT_URL but has no `{EXPECTED_ENTRY}` in extra_hosts, so "
        f"`kuma` does not resolve inside it and every ping it makes is dropped with a logged "
        f"warning nobody reads. See DEPLOY.md, \"Reaching Kuma from a container\".")


@pytest.mark.parametrize("service", sorted(set(SERVICES) - set(PINGERS)))
def test_a_service_that_never_pings_does_not_carry_the_alias(service):
    """Not cosmetic: the alias is the marker that a service talks to Kuma. Left on a service
    that does not, the next reader trying to find every pinger from the compose file finds
    one that is not."""
    entries = SERVICES[service].get("extra_hosts") or []
    assert not any(e.startswith(f"{ALIAS}:") for e in entries), (
        f"{service} declares the {ALIAS} alias but sets no *_HEARTBEAT_URL")


def test_kuma_addr_is_a_default_not_a_stack_wide_guard():
    """`:?` here would stop every compose subcommand on any NAS that has not set it -- for a
    value whose default is correct on the machine this runs on."""
    assert EXPECTED_ENTRY in (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${KUMA_ADDR:?" not in (REPO / "docker-compose.yml").read_text(encoding="utf-8")


def test_kuma_addr_is_documented():
    assert "KUMA_ADDR=host-gateway" in ENV_EXAMPLE
    assert "## Reaching Kuma from a container" in DEPLOY
    assert "KUMA_ADDR" in DEPLOY


def test_no_heartbeat_url_example_hard_codes_a_lan_address():
    """.env.example carries the shape an operator copies. A sample with an address in it is
    how the previous form spread to ten settings in the first place."""
    for path in (".env.example", "DEPLOY.md"):
        for lineno, line in enumerate((REPO / path).read_text(encoding="utf-8").splitlines(), 1):
            if "HEARTBEAT_URL" in line and "http" in line:
                assert "192.168." not in line and "100." not in line, f"{path}:{lineno}: {line}"
