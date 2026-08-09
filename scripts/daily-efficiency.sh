#!/bin/sh
# Nightly conversion-loss update, intended for DSM Task Scheduler (run as root,
# ~03:00 daily -- after daily-savings.sh at 02:00). Back-fills AlphaESS's own
# metered house load and daily energy totals for a rolling window of recent
# complete days, then leaves. Safe to run repeatedly:
#   - the 5-minute metered_power series is written at its own timestamps, so a
#     re-run overwrites rather than duplicates
#   - efficiency.py skips days already written at the current model_version and
#     retries days previously skipped (throttled, gated), so the window is
#     self-healing.
#
# 03:00 rather than just after midnight: AlphaESS finalises a day's totals some
# minutes into the next one, and returns HTTP 200 with a null or all-zero
# payload until it has. efficiency.py refuses to store that, so an early run
# would simply waste the window.
#
# Deliberately a separate script from daily-savings.sh rather than two more
# lines in it. That one runs under `set -eu` too, so a throttled AlphaESS API
# here would abort it before pricing.py runs -- coupling the loss analysis,
# which is a nice-to-have, to the job that produces the money figure.
#
# The window is 4 days, matching daily-savings.sh, but the cost is different:
# each day is 2 AlphaESS API calls at ALPHAESS_MIN_REQUEST_INTERVAL_S apart,
# sharing an appId rate budget with the live 30 s poll loop. Widening it makes
# every night more expensive; a one-off catch-up belongs in a manual
# `--backfill` run, not here.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"
WINDOW_DAYS=4   # reprocess yesterday plus the 3 days before it

cd "$REPO_DIR"

DC="docker compose"

# Compute the local (Europe/Amsterdam) date window inside the container so the
# TZ is correct regardless of the host clock. END = yesterday (most recent
# complete day), START = END - (WINDOW_DAYS - 1).
DATES=$($DC run --rm --no-deps -e WINDOW_DAYS="$WINDOW_DAYS" collector python -c "
import datetime as d, zoneinfo as z, os
t = d.datetime.now(z.ZoneInfo('Europe/Amsterdam')).date()
end = t - d.timedelta(days=1)
start = end - d.timedelta(days=int(os.environ['WINDOW_DAYS']) - 1)
print(start, end)
")
START=$(echo "$DATES" | awk '{print $1}')
END=$(echo "$DATES" | awk '{print $2}')

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-efficiency: processing $START .. $END"

# No heartbeat ping from this script, on purpose. efficiency.py pushes it
# itself, after a row actually lands -- a script-level "exit 0 means healthy"
# ping would report success for a run where every day failed the quality gate
# and nothing was written. If Python dies before it can push, no ping arrives
# and the Kuma Push monitor trips on its own, which is what a dead-man's switch
# is for.
$DC run --rm collector python efficiency.py --backfill "$START" "$END"

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-efficiency: done"
