#!/bin/sh
# Nightly InfluxDB backup, intended for DSM Task Scheduler (run as root,
# ~01:00 daily, ahead of the 02:00 battery-savings job). Runs `influx backup`
# into a dated subfolder of BACKUP_HOST_DIR (bind-mounted into the influxdb
# container at /backups -- see docker-compose.yml), writes a host-side trigger
# file so Synology Cloud Sync actually notices the result, then prunes local
# backups older than BACKUP_RETENTION_DAYS. See BACKUP-DATABASE.MD for the full
# design, including why the trigger file is needed.
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

# Normalised once. The chown and prune below used the bare variable while only
# the dated path carried the default, so an unset BACKUP_HOST_DIR half-worked
# and then failed under `set -u` further down.
BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-./backups}"
OWNER="$(stat -c '%U:%G' "$BACKUP_HOST_DIR")"

TS=$(date +%Y-%m-%d)
DEST="$BACKUP_HOST_DIR/$TS"

# influx backup timestamps every file it writes rather than overwriting, so a
# re-run into a non-empty dated folder just piles up extra full snapshots
# instead of replacing today's. It has to be emptied -- but NOT with
# `rm -rf "$DEST"`.
#
# Deleting the folder is a *host* operation, and Cloud Sync sees those (see the
# note below): it dutifully deletes the folder from Drive. The replacement is
# then written from inside the container, which Cloud Sync does not see, so the
# net effect of a re-run is to remove a good backup from the cloud and upload
# nothing in its place. Emptying in place keeps the folder on both sides.
mkdir -p "$DEST"
find "$DEST" -mindepth 1 -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: backing up to /backups/$TS"

$DC exec -T influxdb influx backup "/backups/$TS" \
  --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"

# Synology Cloud Sync does not notice anything written from inside a container
# through the bind mount -- not the dated folder, not the files in it. Waiting
# does not help; a folder written this way is still unsynced days later. But a
# single *host*-written file inside that folder makes Cloud Sync enumerate it
# and upload everything already there, which is how this was diagnosed: adding
# one file by hand pushed the whole day's backup to Drive at once.
#
# So the last thing written is written by this script, on the host, inside the
# folder. It is the trigger, not the payload -- its contents do not matter, but
# it must be a new write on every run.
#
# Ordinary permissions were never the problem: the folders come out 0755 and
# are world-readable throughout. The chown below still matters so the files
# belong to the account the sync client runs as, but it is not what makes them
# visible.
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$DEST/BACKUP-COMPLETE.txt"

# influx backup runs as root inside the container (no user: pin in
# docker-compose.yml), and the bind mount carries that uid straight through to
# the host, so the dated folder lands root:root. Re-own it to match whoever
# owns the parent BACKUP_HOST_DIR (the account the Drive sync client runs as).
# Covers the trigger file too, hence -R after writing it.
chown -R "$OWNER" "$DEST"

# Pruning runs on the host path directly -- this script runs on the NAS host,
# not in the container, so BACKUP_HOST_DIR is a normal filesystem path here.
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: done"
