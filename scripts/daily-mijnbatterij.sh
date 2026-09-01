#!/bin/sh
# Nightly publication of finished days to mijnbatterij.nl, intended for DSM Task
# Scheduler (run as root, ~03:30 daily -- after daily-savings.sh at 02:00 and
# daily-efficiency.sh at 03:00, both of which write the rows this reads).
#
# WHAT THIS IS FOR. The mijnbatterij container publishes today to /api/live
# every 5 minutes; nothing published a *finished* day until this script. Without
# it the platform's record of yesterday is whatever the last live snapshot
# before midnight happened to carry, which is the day truncated at the last
# submission -- and a day whose daily_cost row lands at 02:00, or is repaired
# three nights later, never reaches the platform at all.
#
# It is `--monthly`, not a new code path: that flag already walks a month up to
# yesterday, prefers the stored daily_energy/daily_cost rows, integrates
# power_readings for days that have none, flags what it cannot stand behind, and
# never sends `finalized`. Re-running it is the documented repair, so running it
# every night is the same operation on a schedule.
#
# WHICH MONTHS. The month of *yesterday*, not of today -- on the 1st those
# differ, and yesterday's is the one holding the day that just finished. The
# previous month is added back for the first HEAL_DAYS days of a month, because
# daily-savings.sh has a 4-day self-healing window: a day skipped for late
# prices can be written two nights later, and by then the month-of-yesterday
# rule alone would have stopped looking at it.
#
# NO HEARTBEAT PING FROM HERE, on purpose, and unlike daily-efficiency.sh there
# is nothing in Python to push one either. MIJNBATTERIJ_HEARTBEAT_URL belongs to
# the live loop, whose Kuma monitor expects a push every 5 minutes; a nightly
# job pushing to the same monitor would hold it green through a dead loop. This
# job's failure surface is DSM's own task-failure notification.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"
HEAL_DAYS=4   # keep in step with WINDOW_DAYS in daily-savings.sh

cd "$REPO_DIR"

DC="docker compose"

# Opt-in, like the service itself. Without a key mijnbatterij.py exits 1 rather
# than idling (a --monthly run is someone waiting for an answer, not a daemon),
# and a scheduled task that fails every night for a feature nobody switched on
# is noise that trains the operator to ignore the notification.
# Quotes stripped because compose strips them too, so MIJNBATTERIJ_API_KEY=""
# is an empty key rather than a two-character one.
KEY=$(sed -n 's/^MIJNBATTERIJ_API_KEY=//p' .env | head -1 | tr -d "\"'")
if [ -z "$KEY" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') daily-mijnbatterij: MIJNBATTERIJ_API_KEY not set, nothing to publish"
    exit 0
fi

# Computed inside the container so the TZ is Europe/Amsterdam regardless of the
# host clock, exactly as daily-savings.sh does its window.
MONTHS=$($DC run --rm --no-deps -e HEAL_DAYS="$HEAL_DAYS" collector python -c "
import datetime as d, zoneinfo as z, os
y = d.datetime.now(z.ZoneInfo('Europe/Amsterdam')).date() - d.timedelta(days=1)
months = [y.replace(day=1)]
if y.day <= int(os.environ['HEAL_DAYS']):
    months.insert(0, (y.replace(day=1) - d.timedelta(days=1)).replace(day=1))
print(' '.join(m.strftime('%Y-%m') for m in months))
")

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-mijnbatterij: publishing $MONTHS"

# Unquoted on purpose: MONTHS is one or two YYYY-MM tokens and --monthly takes
# them as separate arguments.
# shellcheck disable=SC2086
$DC run --rm mijnbatterij python mijnbatterij.py --monthly $MONTHS

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-mijnbatterij: done"
