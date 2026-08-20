"""The dispatch service's deployment shape. DESIGN-dispatch.md section 10.

These read `docker-compose.yml`, the Dockerfile and the entrypoint as text rather than booting
anything. That is the point: every property here is one an edit can quietly undo, and each of
them costs real money or real hardware risk the first time nobody notices.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
DISPATCH = COMPOSE["services"]["dispatch"]
DOCKERFILE = (REPO / "dispatch" / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (REPO / "dispatch" / "entrypoint.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")


def test_the_service_starts_in_dry_run():
    """Going live is an operator action taken while watching the dashboard, with the AlphaESS
    app's own price control already off. A `--live` that arrives by merge instead is two
    controllers fighting over the same registers on somebody else's schedule."""
    assert "--live" not in DISPATCH["command"], (
        "dispatch would start writing to the inverter on deploy -- see the plan's "
        "verification steps 6 and 7")
    # Every service, not just this one: the flag would do the same damage smuggled in
    # anywhere that can reach the inverter. Checked against the parsed commands rather than
    # the file text, because the comments around them discuss `--live` on purpose.
    for name, svc in COMPOSE["services"].items():
        assert "--live" not in str(svc.get("command", "")), (
            f"service {name} would start writing to the inverter on deploy -- going live "
            f"has to be a setting, or this very test fails on every branch from the moment "
            f"the operator performs it")


def test_going_live_is_a_setting_rather_than_an_edit_to_a_tracked_file():
    """The guard above is only honest if there is another way to go live.

    Without one it is not a safety property, it is an instruction to delete a test -- the go-live
    checklist said to edit `command:` in tracked compose, which would have made `pytest` fail
    permanently the day the battery went live. So the flag comes from an environment variable
    that is unset by default, and `entrypoint.sh` is what appends it.

    It cannot come through compose's `command:`: scheduler.py's parser takes only optional
    flags, so an empty `${DISPATCH_LIVE_ARG:-}` arrives as a positional and argparse exits 2
    before the loop starts. That is why the gate lives in the shell.
    """
    assert "DISPATCH_LIVE" in DISPATCH["environment"], (
        "the dispatch service cannot see the go-live switch")
    assert DISPATCH["environment"]["DISPATCH_LIVE"].startswith("${DISPATCH_LIVE:-"), (
        "the switch must default to dry run when the variable is absent from .env")
    assert "DISPATCH_LIVE" in ENTRYPOINT and "--live" in ENTRYPOINT, (
        "nothing appends --live, so the service can never be taken live")
    assert "$LIVE" in ENTRYPOINT and '"$LIVE"' not in ENTRYPOINT, (
        "the appended flag must be unquoted -- quoting it passes an empty string as a "
        "positional argument, which is the failure this mechanism exists to avoid")


def test_the_env_example_does_not_ship_the_live_switch_preloaded():
    """`cp .env.example .env` is how a deployment starts. A `DISPATCH_LIVE=0` sitting there
    pre-filled is one keystroke from a battery nobody is watching; commented out, going live
    stays a deliberate act."""
    live = [ln for ln in ENV_EXAMPLE.splitlines() if ln.startswith("DISPATCH_LIVE")]
    assert not live, f"the live switch shipped uncommented in .env.example: {live}"


def test_there_is_exactly_one_dispatch_service_and_it_does_not_scale():
    """The inverter accepts ONE Modbus TCP connection. A second replica does not degrade, it
    takes the connection away from the first."""
    modbus = [name for name, svc in COMPOSE["services"].items()
              if "dispatch" in str(svc.get("build", ""))]
    assert modbus == ["dispatch"]
    assert "deploy" not in DISPATCH, "no replica count on the service holding :502"
    assert "scale" not in DISPATCH


def test_slots_live_on_a_volume_not_in_the_image():
    """A rebuild must not discard the schedule the battery is currently following."""
    assert "alphaess-dispatch-data:/data" in DISPATCH["volumes"]
    assert "alphaess-dispatch-data" in COMPOSE["volumes"]


def test_the_service_restarts_and_rotates_its_logs():
    assert DISPATCH["restart"] == "unless-stopped"
    assert DISPATCH["logging"]["options"]["max-size"], "unbounded logs on the NAS"


def test_it_reads_the_planning_bucket_with_its_own_token():
    """A token spanning both buckets exists only for this service; sharing the collector's
    would widen that one instead."""
    env = DISPATCH["environment"]
    assert "INFLUX_TOKEN_DISPATCH" in env["INFLUX_TOKEN"]
    assert env["PLANNING_BUCKET"].startswith("${PLANNING_BUCKET")
    assert "INFLUX_TOKEN_DISPATCH" in ENV_EXAMPLE


def test_the_token_has_no_fallback():
    """`:-` would let a missing token fall back to something plausible. `:?` fails the deploy
    instead, which is the lesson of the scoped-token migration."""
    assert "${INFLUX_TOKEN_DISPATCH:?" in (REPO / "docker-compose.yml").read_text()


# Kept OUT of the image on purpose, and the only two that are. `corpus.py` reads archived
# household plan data; `test_mode1_negative.py` opens its own Modbus connection, which would
# steal the dispatcher's single one if it were ever run in here by mistake.
NOT_IN_THE_IMAGE = ("corpus.py", "test_mode1_negative.py")


def shipped_modules() -> list[str]:
    """Every module that must reach the image, DERIVED rather than listed.

    It used to be a literal list, and the list is what failed: `slot_publisher.py` was added
    in #113, nobody remembered the Dockerfile, and the suite stayed green because the test
    only ever checked the eight names already written down. The container then died on
    `ModuleNotFoundError` at the first translation of the deploy -- the one place with no test
    and no CI coverage, since `compose-boot` boots Grafana and every unit test imports from
    `dispatch/` on `pythonpath` rather than from the built image.

    Derived from the directory, a new module is in this test the moment it exists. That is the
    same reasoning `test_dispatch_dashboard.conditional_fields()` gives for computing its set:
    a guard you have to remember to update is a guard that eventually is not one.
    """
    return sorted(p.name for p in (REPO / "dispatch").glob("*.py")
                  if p.name not in NOT_IN_THE_IMAGE)


@pytest.mark.parametrize("module", NOT_IN_THE_IMAGE)
def test_the_image_excludes_what_must_not_run_in_it(module):
    """`corpus.py` reads archived household plan data; `test_mode1_negative.py` opens its own
    Modbus connection, which would steal the dispatcher's."""
    assert f"COPY {module}" not in DOCKERFILE
    assert (REPO / "dispatch" / module).exists(), (
        f"{module} is on the exclusion list but does not exist -- a rename would quietly "
        f"turn this guard off and let the file back into the image under its new name")


@pytest.mark.parametrize("module", shipped_modules())
def test_the_image_carries_every_module_it_imports(module):
    assert f"COPY {module}" in DOCKERFILE, (
        f"dispatch/{module} is not COPYed into the image. The container will build fine and "
        f"then die on ModuleNotFoundError the first time something imports it -- add a COPY "
        f"line to dispatch/Dockerfile, or add it to NOT_IN_THE_IMAGE and say why")


def test_there_are_modules_to_check():
    """Guards the guard: a moved directory would make the parametrised test vacuous rather
    than failing, which is the one way a derived set is worse than a literal one."""
    assert len(shipped_modules()) >= 8


def test_the_healthcheck_never_opens_a_modbus_connection():
    """Section 6.2: any inverter-facing check must be self-reported. `--alive` reads the
    heartbeat file only."""
    assert "--alive" in DOCKERFILE
    assert "--ip" not in DOCKERFILE


def test_the_scheduler_is_the_process_that_receives_sigterm():
    """`release on exit` only runs if SIGTERM reaches the scheduler. Without the exec, the
    shell is PID 1 and `docker compose stop` leaves a command live until its dead man's switch
    expires."""
    assert "exec python -u /app/scheduler.py" in ENTRYPOINT


def test_release_on_exit_gets_longer_than_the_default_grace_period():
    """The other half of the SIGTERM story, and the half that is easy to have wrong.

    Reaching the scheduler is necessary but not sufficient: the release it then performs is a
    Modbus write, and against an unresponsive inverter pymodbus spends ~12 s (3 s x 3 retries)
    before giving up. Docker's default grace period is 10 s, so `stop` would SIGKILL the
    process mid-release and leave the command live until the 300 s dead man's switch expired
    -- in the one situation where somebody has decided they want it to stop NOW.

    Asserted as a floor rather than an exact string so raising it later does not fail here.
    """
    grace = DISPATCH["stop_grace_period"]
    assert grace.endswith("s"), f"expected seconds, got {grace!r}"
    assert int(grace[:-1]) >= 15, (
        "a Modbus release against a slow inverter needs longer than pymodbus's own "
        "retry ladder, which is about 12 s")


def test_the_container_user_has_a_pinned_uid():
    """Docker seeds a named volume's ownership from the image only when it CREATES the volume.
    `alphaess-dispatch-data` already exists on the NAS, chowned to the UID `useradd` happened
    to pick. An implicit UID means a later edit adding a user above it shifts this one, and the
    rebuilt container cannot write slots.json to its own volume."""
    assert "--uid 1000" in DOCKERFILE


def test_the_refresh_interval_is_validated_before_it_reaches_sleep():
    """Under `set -e` a bad TRANSLATE_INTERVAL_S kills the background loop but NOT the
    container: the scheduler keeps dispatching as PID 1 against a slots.json that silently
    stops refreshing. One typo in a compose variable is enough to get there."""
    assert "TRANSLATE_INTERVAL_S" in ENTRYPOINT
    assert "*[!0-9]*" in ENTRYPOINT, "the interval reaches `sleep` unvalidated"
    assert "-gt 0" in ENTRYPOINT, (
        "a digits-only glob passes '00', which `sleep` treats as no delay at all -- the "
        "hourly loop becomes a tight loop against InfluxDB. Zero needs a numeric test")
    assert "sleep \"$INTERVAL\" ||" not in ENTRYPOINT, (
        "`|| true` keeps the loop alive by removing the delay -- it would re-query InfluxDB "
        "as fast as the NAS can answer")


def test_a_failed_first_translation_does_not_stop_the_container():
    """A stale slots.json is a monitored, gracefully-degrading state. A container that refuses
    to start because InfluxDB blinked is a worse failure than the one it avoids."""
    assert "set -eu" in ENTRYPOINT, "the entrypoint should still fail on unset variables"
    line = next(line for line in ENTRYPOINT.splitlines()
                if line.startswith("python -u /app/translate.py"))
    assert line.rstrip().endswith("|| \\"), "the initial translation must be non-fatal"


def test_the_refresh_interval_default_is_the_same_number_in_both_places():
    """The default lives twice: compose passes it in, and the entrypoint falls back to its own
    value when the variable is absent entirely. They only agree by hand.

    Worth pinning because disagreement is invisible in the normal case - compose always sets
    the variable, so the entrypoint's own default is only reached when someone runs the image
    directly, which is exactly when nobody is comparing the two. The number also has to stay
    well under the planner's cadence: at 3600 this ran a measured 46 minutes behind and a plan
    could expire before it was ever read (2026-08-19)."""
    import re
    composeDefault = re.search(
        r"TRANSLATE_INTERVAL_S:\s*\$\{TRANSLATE_INTERVAL_S:-(\d+)\}",
        (REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    entrypointDefault = re.search(r'TRANSLATE_INTERVAL_S:-(\d+)', ENTRYPOINT)
    assert composeDefault and entrypointDefault, "the interval default moved or was renamed"
    assert composeDefault.group(1) == entrypointDefault.group(1), (
        f"compose defaults to {composeDefault.group(1)}s but the entrypoint falls back to "
        f"{entrypointDefault.group(1)}s")
    assert int(composeDefault.group(1)) <= 900, (
        "the translator must read a new plan well inside the planner's hourly cadence, or a "
        "plan can expire before it is read")
