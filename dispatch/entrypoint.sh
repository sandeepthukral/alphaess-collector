#!/bin/sh
# One container, two jobs. DESIGN-dispatch.md section 10.
#
# The translator is a periodic batch job and the dispatcher is a 60 s control loop, but they
# ship as one service because `slots.json` must never leave this container's own volume and
# because exactly one process may ever hold the inverter's single Modbus connection. A second
# container would either duplicate that connection or need a bind mount across compose
# projects; both were rejected in section 10.
#
# The translator runs FIRST and in the FOREGROUND once, so a fresh container has slots before
# it has a Modbus connection. Its failure is deliberately not fatal: a stale slots.json is a
# monitored, gracefully-degrading state (monitor #4) and a container that refuses to start
# because InfluxDB was briefly down would be a worse failure than the one it is avoiding.
#
# Then the translator loops in the background and `exec` hands the container to the scheduler,
# so PID 1 is the thing that talks to the hardware and SIGTERM reaches it directly -- which is
# what makes `release on exit` work on `docker compose stop`.
set -eu

SLOTS="${SLOTS_PATH:-/data/slots.json}"
# Hourly, not three-hourly, although the planner runs every 3 h. A fixed-interval loop is not
# aligned to the planner's schedule, so a 3 h loop can sit up to three hours behind a plan that
# has already landed. The query is one Influx read; running it four times as often costs
# nothing and keeps `slots.json` within an hour of the newest plan.
INTERVAL="${TRANSLATE_INTERVAL_S:-3600}"

# Validated, because `set -e` makes a bad value fatal to the refresh loop and NOT to the
# container: `sleep abc` exits non-zero, the subshell dies, and the scheduler keeps dispatching
# PID 1 as if nothing happened -- against a slots.json that now silently never refreshes. That
# is the worst shape this failure could take, and a typo in one compose variable is enough.
#
# Note this is not `sleep ... || true`, which is the obvious fix and the wrong one: it keeps
# the loop alive by turning the delay into a no-op, and the translator would then re-query
# InfluxDB as fast as the NAS can answer.
#
# Two checks, in this order and not one: the glob rejects anything non-numeric, and only then
# is `[ -gt ]` safe to run -- it errors out on `abc` under `set -e`, which is the failure this
# guard exists to prevent. The numeric test is what catches `00`, which a literal `|0)` pattern
# happily passes and `sleep` then treats as no delay at all.
valid=yes
case "$INTERVAL" in
    ''|*[!0-9]*) valid=no ;;
    *) [ "$INTERVAL" -gt 0 ] || valid=no ;;
esac
if [ "$valid" = no ]; then
    echo "TRANSLATE_INTERVAL_S='$INTERVAL' is not a positive integer -- using 3600" >&2
    INTERVAL=3600
fi

# Absolute paths, and the working directory is /data rather than /app: the audit log and the
# heartbeat file are opened relative to the cwd, and both belong on the volume.
python -u /app/translate.py --slots "$SLOTS" || \
    echo "initial translation failed -- starting anyway, see monitors 2 and 3" >&2

(
    while true; do
        sleep "$INTERVAL"
        python -u /app/translate.py --slots "$SLOTS" || true
    done
) &

# Going live is a SETTING, not an edit to a tracked file. The obvious mechanism -- putting
# `${DISPATCH_LIVE_ARG:-}` in the compose `command:` -- does not work: scheduler.py's parser
# takes only optional flags, so the empty string arrives as a positional and argparse exits 2
# with "unrecognized arguments" before the loop ever starts. Verified, not assumed.
#
# So the flag is appended here instead, and $LIVE is deliberately UNQUOTED: an empty value has
# to contribute no argv element at all, which is exactly what quoting would prevent.
LIVE=""
case "${DISPATCH_LIVE:-0}" in
    1|true|yes|on)
        LIVE="--live"
        echo "DISPATCH_LIVE=${DISPATCH_LIVE} -- the dispatcher will WRITE to the inverter" >&2
        ;;
esac

exec python -u /app/scheduler.py --slots "$SLOTS" $LIVE "$@"
