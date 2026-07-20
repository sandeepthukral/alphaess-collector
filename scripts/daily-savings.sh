#!/bin/sh
# Nightly battery-savings update, intended for DSM Task Scheduler (run as root,
# ~02:00 daily). Fetches Frank Energie prices and computes per-day savings for a
# rolling window of recent complete days, then leaves. Safe to run repeatedly:
#   - prices.py writes are idempotent (same hourly timestamps overwrite)
#   - pricing.py skips days already written and retries days previously skipped
#     (late-published prices / low coverage), so the window is self-healing.
#
# Always invokes docker compose with BOTH files so InfluxDB keeps its
# `shared-grafana-net` attachment + `influxdb` network alias (a bare
# `docker compose` recreates it off that network and breaks the Grafana
# datasource). See DEPLOY.md.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"
WINDOW_DAYS=4   # reprocess yesterday plus the 3 days before it

cd "$REPO_DIR"

DC="docker compose -f docker-compose.yml -f docker-compose.nas.yml"

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

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-savings: processing $START .. $END"

$DC run --rm collector python prices.py  --backfill "$START" "$END"
$DC run --rm collector python pricing.py --backfill "$START" "$END"

echo "$(date '+%Y-%m-%d %H:%M:%S') daily-savings: done"
