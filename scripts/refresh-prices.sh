#!/bin/sh
# Keep the *forward* end of `market_price` current, for the Battery Plan
# dashboard. Intended for DSM Task Scheduler (run as root, every 3 hours).
#
# This exists because daily-savings.sh cannot do it. That job's window ends at
# yesterday, deliberately: pricing.py scores complete days, and today is not one.
# So nothing was ever fetching today or tomorrow, and the plan dashboard - whose
# whole time range is now-6h..now+36h - had no price to draw. That is not a
# visible failure, because the panel renders perfectly well while empty.
#
# prices.py with no arguments fetches yesterday, today and tomorrow. Tomorrow's
# day-ahead is not published until early afternoon, and an unpublished day is
# skipped rather than fatal, which is why this runs repeatedly through the day
# instead of once: the first run after publication is the one that lands it.
#
# Safe to run as often as you like - writes are idempotent, same slot
# timestamps overwrite.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"

cd "$REPO_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') refresh-prices: fetching yesterday..tomorrow"

docker compose run --rm collector python prices.py

echo "$(date '+%Y-%m-%d %H:%M:%S') refresh-prices: done"
