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

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: backing up to /backups/$TS"

$DC exec -T influxdb influx backup "/backups/$TS" \
  --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"

# Pruning runs on the host path directly -- this script runs on the NAS host,
# not in the container, so BACKUP_HOST_DIR is a normal filesystem path here.
find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d \
  -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-influxdb: done"
