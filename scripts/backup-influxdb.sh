#!/bin/sh
# Nightly InfluxDB backup, intended for DSM Task Scheduler (run as root,
# ~01:00 daily, ahead of the 02:00 battery-savings job). Runs `influx backup`
# into a staging folder under BACKUP_HOST_DIR (bind-mounted into the influxdb
# container at /backups -- see docker-compose.yml), then archives that folder
# from the *host* into a single dated .tgz, and prunes archives older than
# BACKUP_RETENTION_DAYS. See BACKUP-DATABASE.MD for the full design, including
# why the host has to be the one writing the archive.
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
STAGING="$BACKUP_HOST_DIR/.staging"
DEST="$BACKUP_HOST_DIR/influxdb-$TS.tgz"

# WHY THE BACKUP IS STAGED AND THEN TARRED, RATHER THAN WRITTEN WHERE IT LANDS:
#
# Synology Cloud Sync does not observe anything written from inside a container
# through a bind mount. Not the folder, not the files in it, ever -- a backup
# written that way is still absent from Drive days later, so this is not a race
# that a delay could win. Host-side writes to the same tree *are* observed, and
# individually: `rm -rf` here propagated a deletion to Drive immediately, and
# backup-grafana.sh's host-written .tgz has always synced.
#
# An earlier attempt exploited that by writing one host-side marker file inside
# the container-written folder, on the theory that it would make Cloud Sync
# enumerate the folder. It does not. The marker file synced on its own and the
# fifteen backup files beside it did not, which is the whole answer: Cloud Sync
# uploads the writes it saw and nothing else.
#
# So the payload itself has to be a host write. `influx backup` still has to
# run in the container -- it is the container's database -- but its output goes
# to a staging folder that is never expected to sync, and the host then writes
# the one file that is: the archive. That is the same shape backup-grafana.sh
# has always used, which is the only part of this tree with an unbroken record
# of reaching Drive.
#
# Ordinary permissions were never involved: the folders came out 0755 and
# world-readable throughout. The chown below still matters so the archive
# belongs to the account the sync client runs as, but it is not what makes it
# visible.

# influx backup timestamps every file it writes rather than overwriting, so a
# re-run into a non-empty folder piles up extra full snapshots instead of
# replacing them. Empty the staging folder rather than recreating it, and empty
# it again afterwards -- it is scratch space, and leaving a full uncompressed
# copy of the database lying around doubles what this job costs on disk.
mkdir -p "$STAGING"
find "$STAGING" -mindepth 1 -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: backing up to $DEST"

$DC exec -T influxdb influx backup "/backups/.staging" \
  --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"

# The host write. Overwrites today's archive on a re-run, so the job stays
# idempotent without any deletion propagating to Drive first.
tar -czf "$DEST" -C "$STAGING" .

find "$STAGING" -mindepth 1 -exec rm -rf {} +

# influx backup runs as root inside the container (no user: pin in
# docker-compose.yml) and tar here runs as root too, so re-own the archive to
# match whoever owns BACKUP_HOST_DIR (the account the Drive sync client runs
# as).
chown "$OWNER" "$DEST"

# Pruning runs on the host path directly -- this script runs on the NAS host,
# not in the container, so BACKUP_HOST_DIR is a normal filesystem path here.
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type f -name 'influxdb-*.tgz' \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -f {} +

# Until 2026-08-10 backups were dated folders rather than archives. Ages the
# leftovers out on the same schedule; delete this once none remain.
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d -name '20*' \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: done"
