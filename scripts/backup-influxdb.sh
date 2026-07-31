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

TS=$(date +%Y-%m-%d)

# influx backup timestamps every file it writes rather than overwriting, so a
# re-run into a non-empty dated folder just piles up extra full snapshots
# instead of replacing today's. Clear it first so a re-run (manual test, retry
# after a failure) replaces rather than duplicates.
rm -rf "${BACKUP_HOST_DIR:-./backups}/$TS"

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
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: done"
