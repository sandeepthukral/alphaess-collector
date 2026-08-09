#!/bin/sh
# Restore drill: proves a nightly InfluxDB archive actually restores, without
# going anywhere near the live database. Run by hand -- see DEPLOY.md,
# "Verifying a backup restores".
#
#   ./scripts/verify-influxdb-backup.sh                  # newest archive
#   ./scripts/verify-influxdb-backup.sh /path/to/x.tgz   # a specific one
#
# WHY THIS EXISTS: a backup nobody has restored is a backup nobody knows works.
# Everything before this point verified that a file appeared and that it
# reached Drive -- neither of which says the contents can be read back. The
# archive is also a tar of `influx backup`'s output rather than that output
# directly (see BACKUP-DATABASE.MD), and that round trip had never been
# exercised.
#
# WHY A THROWAWAY CONTAINER: `influx restore --full` replaces the target
# server's entire key-value store -- buckets, orgs, users, tokens, all of it.
# Pointed at the running stack it would overwrite the very data you are trying
# to prove you could recover, which is a rehearsal that can only cost you.
# So the drill starts its own influxdb container with its own anonymous volume,
# publishes no ports, joins no compose network, and is destroyed on the way
# out. Nothing here touches alphaess-influxdb-data.
#
# The archive is extracted to a mktemp dir, NOT to $BACKUP_HOST_DIR/.staging as
# the manual restore in DEPLOY.md does. Extraction is a host write, and a host
# write inside the synced folder would push a second full copy of the backup up
# to Drive -- a drill should leave no trace.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
# This script is manual, but it is run over SSH on the NAS where the same
# applies.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

# Overridable, unlike the nightly scripts' hardcoded path: this one is also run
# from a checkout on a laptop when the drill itself is being changed.
REPO_DIR="${REPO_DIR:-/volume1/docker/alphaess-collector}"

cd "$REPO_DIR"

set -a
. ./.env
set +a

BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-./backups}"
INFLUX_ORG="${INFLUX_ORG:-home}"
INFLUX_BUCKET="${INFLUX_BUCKET:-alphaess}"

# How far back to look for data in the restored copy. A backup taken last
# night contains months, so anything up to the retention period would do; a
# week is short enough that an empty result means something real.
DRILL_RANGE="${DRILL_RANGE:-7d}"

NAME="alphaess-restore-drill"

# The drill server is initialised with production's own org and admin token,
# which looks odd for a throwaway and is the whole trick. `influx restore
# --full` replaces the key-value store -- including every token -- partway
# through, and then carries on to the SQL snapshot using the credentials it was
# invoked with. Initialise the server with anything else and the restore dies
# halfway:
#
#   INFO: Restoring KV snapshot
#   INFO: Restoring SQL snapshot
#   Error: failed to restore SQL snapshot: 401 Unauthorized
#
# leaving a half-restored server. Init with the backup's own token and the
# credentials are the same on both sides of that switch, so the command that
# invalidates them never notices. Restoring into the real stack works for the
# same reason without anyone having to think about it: its admin token *is*
# this token.
#
# Nothing secret reaches the drill container that is not already in the backup
# it is restoring.

ARCHIVE="${1:-}"
if [ -z "$ARCHIVE" ]; then
    ARCHIVE=$(ls -1t "$BACKUP_HOST_DIR"/influxdb-*.tgz 2>/dev/null | head -n 1 || true)
    if [ -z "$ARCHIVE" ]; then
        echo "verify-influxdb-backup: no influxdb-*.tgz in $BACKUP_HOST_DIR" >&2
        exit 1
    fi
fi
if [ ! -f "$ARCHIVE" ]; then
    echo "verify-influxdb-backup: no such archive: $ARCHIVE" >&2
    exit 1
fi

# Drill against the exact image the stack runs, not a floating tag: a restore
# proved under a different InfluxDB build proves less than it appears to. Falls
# back to the compose default when the stack is down, which is also the case
# where you are most likely to be running this for real.
CID=$(docker compose ps -aq influxdb 2>/dev/null || true)
IMAGE=""
if [ -n "$CID" ]; then
    IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CID" 2>/dev/null || true)
fi
IMAGE="${IMAGE:-influxdb:2}"

WORK=$(mktemp -d)

cleanup() {
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "verify-influxdb-backup: testing $ARCHIVE against $IMAGE"

tar -xzf "$ARCHIVE" -C "$WORK"

# A leftover from an interrupted earlier run would make `docker run` fail on
# the name; it is ours either way.
docker rm -f -v "$NAME" >/dev/null 2>&1 || true

docker run -d --name "$NAME" \
    -e DOCKER_INFLUXDB_INIT_MODE=setup \
    -e DOCKER_INFLUXDB_INIT_USERNAME=drill \
    -e DOCKER_INFLUXDB_INIT_PASSWORD=drillpassword \
    -e DOCKER_INFLUXDB_INIT_ORG="$INFLUX_ORG" \
    -e DOCKER_INFLUXDB_INIT_BUCKET=drill \
    -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="$INFLUX_TOKEN" \
    -v "$WORK":/drill:ro \
    "$IMAGE" >/dev/null

echo "verify-influxdb-backup: waiting for the drill server"
i=0
until docker exec "$NAME" influx ping >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "verify-influxdb-backup: drill server never came up" >&2
        docker logs --tail 30 "$NAME" >&2 || true
        exit 1
    fi
    sleep 2
done

# No --org here, deliberately: `--full` restores every org in the backup and
# the CLI rejects the combination outright ("--full restore cannot be limited
# to a single org or bucket"). DEPLOY.md's restore snippet passed both until
# this drill was written, which is exactly the kind of thing a rehearsal is
# for -- the documented procedure would have failed on its first command, at
# the worst possible moment.
echo "verify-influxdb-backup: restoring"
docker exec "$NAME" influx restore /drill \
    --full --token "$INFLUX_TOKEN"

q() {
    docker exec "$NAME" influx query --raw \
        --org "$INFLUX_ORG" --token "$INFLUX_TOKEN" "$1"
}

# Reading the annotated CSV: the value is the last field of the last data row.
# Only ever one row here, every query below collapsing to a single number.
last_value() {
    tr -d '\r' | awk -F, 'NF > 1 && $1 == "" && $2 != "result" { v = $NF } END { print v }'
}

echo "verify-influxdb-backup: checking the restored copy"

if ! docker exec "$NAME" influx bucket list \
        --org "$INFLUX_ORG" --token "$INFLUX_TOKEN" \
        --name "$INFLUX_BUCKET" >/dev/null 2>&1; then
    echo "verify-influxdb-backup: FAIL -- bucket $INFLUX_BUCKET is not in the restored copy" >&2
    exit 1
fi

# Both queries map every row to a constant int before grouping. Without that,
# group() has to merge tables whose _value types differ -- this bucket holds
# floats and strings side by side -- and count()/max() refuse a time column
# outright ("unsupported aggregate column type time"). Mapping first makes the
# merged table trivially uniform, and asks only "how many rows, and how recent".
COUNT=$(q "from(bucket: \"$INFLUX_BUCKET\")
  |> range(start: -$DRILL_RANGE)
  |> map(fn: (r) => ({_time: r._time, _value: 1}))
  |> group()
  |> count()" | last_value)

NEWEST=$(q "from(bucket: \"$INFLUX_BUCKET\")
  |> range(start: -$DRILL_RANGE)
  |> map(fn: (r) => ({_time: r._time, _value: 1}))
  |> group()
  |> sort(columns: [\"_time\"], desc: true)
  |> limit(n: 1)
  |> map(fn: (r) => ({_value: string(v: r._time)}))" | last_value)

case "$COUNT" in
    '' | 0 | *[!0-9]*)
        echo "verify-influxdb-backup: FAIL -- no points in the last $DRILL_RANGE of the restored $INFLUX_BUCKET (got '$COUNT')" >&2
        exit 1
        ;;
esac

echo "verify-influxdb-backup: PASS"
echo "  bucket:  $INFLUX_BUCKET restored"
echo "  points:  $COUNT in the last $DRILL_RANGE"
echo "  newest:  $NEWEST"
echo "  archive: $ARCHIVE"
