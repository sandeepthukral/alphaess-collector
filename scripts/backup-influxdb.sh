#!/bin/sh
# Nightly InfluxDB backup, intended for DSM Task Scheduler (run as root,
# ~01:00 daily, ahead of the 02:00 battery-savings job). Runs `influx backup`
# into a dated subfolder of BACKUP_HOST_DIR (bind-mounted into the influxdb
# container at /backups -- see docker-compose.yml), then prunes local backups
# older than BACKUP_RETENTION_DAYS. See BACKUP-DATABASE.MD for the full design.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"

cd "$REPO_DIR"

# influx inside the container doesn't automatically see an INFLUX_TOKEN/
# INFLUX_ORG env var (the compose file only sets DOCKER_INFLUXDB_INIT_*
# names), so pull them from .env on the host and pass them as CLI flags.
# INFLUX_TOKEN here must be the admin token specifically -- influx backup
# requires operator-level permissions and rejects scoped tokens.
set -a
. ./.env
set +a

DC="docker compose"

# Normalised once: the rest of the script (chown, prune, pre-create) used the
# bare variable while only the dated path carried the default, so an unset
# BACKUP_HOST_DIR half-worked and then failed under `set -u` further down.
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-./backups}"

TS=$(date +%Y-%m-%d)
DEST="$BACKUP_HOST_DIR/$TS"

# Seconds to wait after creating a dated folder before writing into it. Only
# used on the fallback path below; see the comment there.
SETTLE_S="${BACKUP_SETTLE_SECONDS:-30}"

# influx backup timestamps every file it writes rather than overwriting, so a
# re-run into a non-empty dated folder just piles up extra full snapshots
# instead of replacing today's. It has to be emptied -- but NOT by deleting the
# folder itself.
#
# WHY NOT `rm -rf "$DEST"`: Synology Cloud Sync notices a deletion immediately,
# but a folder that is created and then filled within the same second often
# escapes its watcher entirely, and the day's backup silently never reaches the
# cloud. Deleting the folder and letting `influx backup` recreate it is exactly
# that pattern. Emptying it in place keeps the inode Cloud Sync is already
# watching, so only the files inside change -- which it does handle.
if [ -d "$DEST" ]; then
    find "$DEST" -mindepth 1 -exec rm -rf {} +
else
    # Fallback: the folder should already exist, pre-created by yesterday's run
    # (see the bottom of this script). It will not on the very first run, or
    # after a night the job did not run at all. Create it, own it, and give
    # Cloud Sync a moment to register the watch before any file appears inside.
    # This is a race we are trying to win rather than avoid, which is why it is
    # the fallback and not the normal path.
    mkdir -p "$DEST"
    chown "$(stat -c '%U:%G' "$BACKUP_HOST_DIR")" "$DEST"
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: $TS did not exist;" \
         "waiting ${SETTLE_S}s for Cloud Sync to see it"
    sleep "$SETTLE_S"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: backing up to /backups/$TS"

$DC exec -T influxdb influx backup "/backups/$TS" \
  --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"

# influx backup runs as root inside the container (no user: pin in
# docker-compose.yml), and the bind mount carries that uid straight through to
# the host, so the dated folder lands root:root. That's invisible to the
# Google Drive sync client, which runs as a normal user -- backups were never
# actually reaching Drive. This script already runs as root (Task Scheduler),
# so re-own the fresh folder to match whoever already owns the parent
# BACKUP_HOST_DIR (the account the Drive sync client runs as).
chown -R "$(stat -c '%U:%G' "$BACKUP_HOST_DIR")" "$BACKUP_HOST_DIR/$TS"

# Pruning runs on the host path directly -- this script runs on the NAS host,
# not in the container, so BACKUP_HOST_DIR is a normal filesystem path here.
# Tomorrow's folder, created below, is minutes old and so is never in range.
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +

# Create tomorrow's folder now, empty, so that tomorrow's run finds it already
# there and takes the empty-in-place path above instead of creating a folder
# and filling it a second later. That gives Cloud Sync a full day to notice the
# new directory while there is nothing in it to miss -- which turns the race
# described above into a non-event rather than a shorter race.
#
# The date comes from the influxdb container rather than the host: it is
# Debian-based, so `date -d` accepts a relative offset, whereas the NAS's
# busybox date does not. The container gets TZ from docker-compose.yml, so its
# idea of "tomorrow" matches the host's.
# `|| TOMORROW=""` is load-bearing: without it a failure here would abort the
# script under `set -e` after a perfectly good backup, and the else branch
# below would never run.
TOMORROW=$($DC exec -T influxdb date -d '+1 day' +%Y-%m-%d 2>/dev/null | tr -d '\r') \
    || TOMORROW=""
if [ -n "$TOMORROW" ]; then
    mkdir -p "$BACKUP_HOST_DIR/$TOMORROW"
    chown "$(stat -c '%U:%G' "$BACKUP_HOST_DIR")" "$BACKUP_HOST_DIR/$TOMORROW"
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: pre-created $TOMORROW"
else
    # Not fatal: tomorrow's run falls back to creating the folder itself and
    # waiting out SETTLE_S. Today's backup is already written and owned.
    echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: could not pre-create" \
         "tomorrow's folder; tomorrow's run will fall back to the wait" >&2
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: done"
