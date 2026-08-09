#!/bin/sh
# Runs every nightly backup, as a single DSM Task Scheduler entry (root,
# ~01:00 daily, ahead of the 02:00 battery-savings job). See DEPLOY.md,
# "Backing up InfluxDB" and "Backing up Grafana", for what each one does.
#
# To add a backup: write it as its own scripts/backup-*.sh, then add its
# filename to JOBS below. Order is run order, most important first -- a job
# that hangs stops the ones after it, so the thing you would most regret
# losing goes at the top.
#
# DELIBERATELY NOT `set -e`. That is the entire point of this script existing
# rather than the two commands being pasted into the DSM box: every job runs
# even if an earlier one fails, so a broken Grafana backup cannot cost you the
# database backup. Each job's own script keeps `set -eu` internally and still
# aborts itself properly; only the sequencing here is tolerant.
#
# The exit code is 1 if any job failed, so DSM still reports the task as failed
# and can email about it -- tolerant of failures is not the same as silent
# about them.
set -u

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"

cd "$REPO_DIR"

JOBS="backup-influxdb.sh backup-grafana.sh"

rc=0
failed=""
total=0

for job in $JOBS; do
    total=$((total + 1))
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-all: running $job"
    if "./scripts/$job"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') backup-all: $job ok"
    else
        # Reported here as well as by the job itself: this line is the one that
        # survives in the DSM task log next to the others, so a glance at the
        # tail says which of them broke.
        echo "$(date '+%Y-%m-%d %H:%M:%S') backup-all: $job FAILED (continuing)" >&2
        failed="$failed $job"
        rc=1
    fi
done

if [ "$rc" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-all: done, $total job(s) ok"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-all: done, $total job(s), failed:$failed" >&2
fi

exit "$rc"
