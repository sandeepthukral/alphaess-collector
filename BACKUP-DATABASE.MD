# Nightly InfluxDB backups to the NAS's Google-Drive-synced folder

## Context

InfluxDB currently stores its data only in the named Docker volume
`alphaess-influxdb-data` (`docker-compose.yml:16`). That volume lives under
Docker's own storage area on the NAS and is not touched by the NAS's existing
Google Drive backup, which only covers ordinary shared folders (confirmed
path: `/volume1/web/googleDrive`). The goal is a nightly job that lands a
restorable InfluxDB backup inside that already-backed-up folder, so it rides
along with the existing Drive backup automatically — no new backup
infrastructure to manage.

Because InfluxDB is a live, continuously-written database, we must not copy
the raw volume files directly (risk of a torn/corrupt snapshot). InfluxDB 2.x
ships `influx backup`, which produces a consistent, portable backup while the
server keeps running — that's the mechanism to use, restorable with
`influx restore`.

This follows the exact operational pattern already established by
`scripts/daily-savings.sh` + DSM Task Scheduler (see `DEPLOY.md`'s "Nightly
battery-savings update" section) — same `REPO_DIR`, same `#!/bin/sh` /
`set -eu` style, same Task Scheduler setup shape — so it reads as more of the
same rather than a new pattern.

One deliberate exception to an existing rule: `DEPLOY.md`'s "Scoped tokens"
section says nothing but InfluxDB's first-start init and manual `influx` CLI
work should ever hold the admin `INFLUX_TOKEN`. This backup script is a
second, unattended holder of it — unavoidably. InfluxDB 2.x's `influx
backup`/`influx restore` require an **operator token**; scoped all-access
tokens are documented to fail backup with permission errors. There's no
narrower token this script could use.

Confirmed with the user:
- Google-Drive-synced host path: `/volume1/web/googleDrive` (backups will go
  in a subfolder there, `alphaess-influxdb-backups`, to avoid dumping loose
  dated folders in the Drive root)
- Local retention before pruning: 7 days (Drive keeps its own history/version
  copies independently, so the NAS copy is just a rolling local safety net)
- Schedule: 01:00 daily, ahead of the existing 02:00 battery-savings job

## Changes

### 1. `docker-compose.yml` — bind-mount a backup target into the `influxdb` service

Add a second volume line under `influxdb:` (`docker-compose.yml:15-16`),
alongside the existing named volume, using an env var so the path isn't
hardcoded to this one NAS:

```yaml
volumes:
  - alphaess-influxdb-data:/var/lib/influxdb2
  - ${BACKUP_HOST_DIR:-./backups}:/backups
```

Default `./backups` keeps a non-NAS `docker compose up` working out of the box
(relative to the repo checkout); the NAS `.env` overrides it to the real Drive
path.

### 2. `.env.example` — document the new vars

Add near the InfluxDB section:

```
# Nightly InfluxDB backup (scripts/backup-influxdb.sh via DSM Task Scheduler).
# Host directory bind-mounted into the influxdb container at /backups. Point
# this at a folder your NAS backup tool (e.g. Google Drive sync) already
# covers, so InfluxDB backups ride along automatically.
BACKUP_HOST_DIR=/volume1/web/googleDrive/alphaess-influxdb-backups
# Local backups older than this are pruned after each run (the external
# backup target keeps its own history independently).
BACKUP_RETENTION_DAYS=7
```

### 3. New `scripts/backup-influxdb.sh`

Mirrors `scripts/daily-savings.sh` conventions (`#!/bin/sh`, `set -eu`, fixed
`PATH`, `REPO_DIR=/volume1/docker/alphaess-collector`, `cd` into it, echoed
start/done lines):

- Source `.env` (`set -a; . ./.env; set +a`) to get `INFLUX_ORG`,
  `INFLUX_TOKEN`, `BACKUP_HOST_DIR`, `BACKUP_RETENTION_DAYS` into the script's
  shell — needed because the *host* script (not a container) does the pruning,
  and because `influx` inside the container doesn't automatically see an
  `INFLUX_TOKEN`/`INFLUX_ORG` env var (the compose file only sets
  `DOCKER_INFLUXDB_INIT_*` names), so the token/org must be passed explicitly
  as CLI flags built from values read on the host. `INFLUX_TOKEN` here must be
  the **admin** token specifically — `influx backup` requires operator-level
  permissions and rejects scoped/all-access tokens like
  `INFLUX_TOKEN_COLLECTOR`.
- Run the backup into a dated subfolder so each day is independently
  restorable and prunable. `influx backup` timestamps every file it writes
  rather than overwriting, so a re-run into a non-empty dated folder silently
  doubles the files instead of replacing them (confirmed by testing) — clear
  the folder first so a re-run (manual test, retry after failure) replaces
  today's backup rather than piling up extra full snapshots next to it:
  ```sh
  TS=$(date +%Y-%m-%d)
  rm -rf "${BACKUP_HOST_DIR:-./backups}/$TS"
  docker compose exec -T influxdb influx backup "/backups/$TS" \
    --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"
  ```
- Prune local backups older than `BACKUP_RETENTION_DAYS`, operating directly
  on the host path (the script runs on the NAS host, so no need to do this
  inside the container):
  ```sh
  find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type d \
    -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -rf {} +
  ```
- `chmod +x scripts/backup-influxdb.sh`.

### 4. `DEPLOY.md` — new "Backing up InfluxDB" section

Placed alongside the existing "Battery-savings pricing jobs" section, covering:

- Why `influx backup`/`influx restore` rather than copying the volume (live
  DB; app-consistent portable format).
- Why the script uses the admin `INFLUX_TOKEN` rather than a scoped token
  (operator permissions are required for backup/restore) — flagged as the
  one deliberate exception to the "nothing but init/manual CLI" rule.
- The `BACKUP_HOST_DIR` / `BACKUP_RETENTION_DAYS` env vars and that the
  former must point at a folder the NAS's own backup tool already watches
  (here: `/volume1/web/googleDrive/alphaess-influxdb-backups`).
- DSM Task Scheduler entry, matching the existing nightly job's format:
  - **General**: User = `root`
  - **Schedule**: Daily, first run time `01:00`
  - **Run command**: `/volume1/docker/alphaess-collector/scripts/backup-influxdb.sh`
  - Test once by hand first:
    `sudo /volume1/docker/alphaess-collector/scripts/backup-influxdb.sh`
- Restore procedure (note: verify exact flags for the installed InfluxDB
  version with `docker compose exec influxdb influx restore --help`, since CLI
  flags can shift across versions):
  ```sh
  docker compose exec influxdb influx restore "/backups/<date>" \
    --full --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"
  ```

Also add a one-line callout in the existing "Scoped tokens" section pointing
here — e.g. "the nightly backup script is the one exception; see 'Backing up
InfluxDB' for why" — so that section's "nothing but init/manual CLI" rule
doesn't read as contradicted by an unattended script the reader hasn't seen
yet.

### 5. `README.md`

One-line pointer from the NAS deployment section to the new DEPLOY.md
section, matching how the battery-savings nightly job is already linked.

## Verification

1. `docker compose up -d` (or just recreate `influxdb`) after the compose
   change, then confirm the bind mount landed:
   `docker compose exec influxdb ls /backups` (should exist, empty).
2. Run the new script by hand from the repo root and confirm a dated folder
   with InfluxDB backup manifest/data files appears both inside the container
   (`docker compose exec influxdb ls /backups/<today>`) and on the host at
   `/volume1/web/googleDrive/alphaess-influxdb-backups/<today>`.
3. Sanity-check restore flags against the actual installed CLI:
   `docker compose exec influxdb influx restore --help`.
4. Re-run the script a second time and confirm it's idempotent for the day
   (overwrites or errors cleanly, not silently duplicating) and that the
   retention `find`/prune command does not delete today's fresh folder.
5. Add the DSM Task Scheduler entry and trigger a manual "Run now" once to
   confirm it works under Task Scheduler's environment (minimal `PATH`, `root`
   user), same as was done for the existing battery-savings task.
