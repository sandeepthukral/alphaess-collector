#!/bin/sh
# Nightly Grafana backup, intended for DSM Task Scheduler (run as root, ~00:45
# daily, ahead of the 01:00 InfluxDB backup). Tars the `alphaess-grafana-data`
# volume into a dated archive under GRAFANA_BACKUP_HOST_DIR, then prunes
# archives older than BACKUP_RETENTION_DAYS. See DEPLOY.md, "Backing up
# Grafana".
#
# WHY THIS EXISTS WHEN ALMOST EVERYTHING IS PROVISIONED: datasources,
# dashboards and alert *rules* are provisioned from ./grafana/provisioning, so
# a rebuild from an empty volume restores them by itself. Contact points and
# the notification policy are not -- Grafana has no file provisioning for them
# in this repo, so they exist only in this volume's grafana.db. Lose it and the
# alert rules come straight back while the routing silently does not: every
# rule fires into nothing, which looks exactly like healthy. The admin
# password (GF_SECURITY_ADMIN_PASSWORD binds only at first init), annotations,
# and the installed echarts plugin are in here too.
#
# WHY GRAFANA IS STOPPED FOR IT: grafana.db is SQLite, written live. The same
# argument BACKUP-DATABASE.MD makes against copying InfluxDB's files applies --
# a copy taken while it is being written can be torn. InfluxDB has `influx
# backup` for that; Grafana has no equivalent, so the consistency has to come
# from there being no writer. Stopping it costs a few seconds at 00:45 and
# makes the archive trivially correct. The trap below restarts it even if the
# tar or the prune fails, so a bad night cannot leave Grafana down.
#
# Deliberately a separate script from backup-influxdb.sh rather than two more
# lines in it, for the reason daily-efficiency.sh is separate from
# daily-savings.sh: that one runs under `set -eu` too, and a failure here would
# abort the database backup, which is the one that actually matters.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/alphaess-collector"

cd "$REPO_DIR"

set -a
. ./.env
set +a

DC="docker compose"

DEST="${GRAFANA_BACKUP_HOST_DIR:-./backups-grafana}"
TS=$(date +%Y-%m-%d)

# Resolved from the container rather than hardcoded: Compose prefixes named
# volumes with the project name, so the volume is not simply
# `alphaess-grafana-data` on the Docker side. Done before stopping anything --
# `ps -aq` finds the container either way, but there is no reason to take
# Grafana down and only then discover the mount is missing.
CID=$($DC ps -aq grafana)
if [ -z "$CID" ]; then
    echo "backup-grafana: no grafana container found -- is the stack up?" >&2
    exit 1
fi

SRC=$(docker inspect \
    -f '{{range .Mounts}}{{if eq .Destination "/var/lib/grafana"}}{{.Source}}{{end}}{{end}}' \
    "$CID")
if [ -z "$SRC" ]; then
    echo "backup-grafana: no volume mounted at /var/lib/grafana on $CID" >&2
    exit 1
fi

mkdir -p "$DEST"

# Fires on success and on failure alike. `docker compose start` on an
# already-running container is a no-op, so the explicit start further down does
# not conflict with it.
trap '$DC start grafana >/dev/null 2>&1 || true' EXIT INT TERM

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-grafana: stopping grafana"
$DC stop grafana

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-grafana: archiving to $DEST/grafana-$TS.tgz"
tar -czf "$DEST/grafana-$TS.tgz" -C "$SRC" .

$DC start grafana

# Same lesson as backup-influxdb.sh: files written by a root process are
# invisible to the Google Drive sync client, which runs as a normal user, so
# the backup never actually leaves the NAS. Match whoever owns the parent.
chown "$(stat -c '%U:%G' "$DEST")" "$DEST/grafana-$TS.tgz"

find "$DEST" -maxdepth 1 -mindepth 1 -type f -name 'grafana-*.tgz' \
    -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -f {} +

echo "$(date '+%Y-%m-%d %H:%M:%S') backup-grafana: done"
