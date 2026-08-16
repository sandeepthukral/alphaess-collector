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

exec python -u /app/scheduler.py --slots "$SLOTS" "$@"
