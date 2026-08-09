# Nightly InfluxDB backups to the NAS's Google-Drive-synced folder

Status: **complete and verified on the NAS**, 2026-07-31. All 5 changes below
are live; all verification steps passed (see checkmarks). Two bugs found and
fixed during verification:
- `influx backup` doesn't overwrite same-day files, so a re-run silently
  doubled them — the script now clears the dated folder first (§3).
- The dated backup folder landed `root:root` on the host (the container runs
  `influx backup` as root, and the bind mount carries that uid straight
  through). The script now `chown`s the fresh folder to match whoever owns the
  parent `BACKUP_HOST_DIR` after each run (§3). This was originally recorded as
  the reason backups were not reaching Drive; **that diagnosis was wrong** —
  see below.
- **2026-08-10: backups still were not reaching Drive**, and neither ownership
  nor timing was the cause. Cloud Sync does not observe anything written from
  inside a container through a bind mount. The backup is now staged by the
  container and archived by the *host* into a single dated `.tgz` (§3, "Making
  Cloud Sync notice"). An intermediate fix that same day — a host-written
  trigger file inside the container-written folder — did not work, and is kept
  below as the experiment that pinned down the rule. **Verified on the NAS the
  same day** (step 7).

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
- Run `influx backup` into a fixed staging folder, then have the *host* tar it
  into one dated archive per day, so each day stays independently restorable
  and prunable. `influx backup` timestamps every file it writes rather than
  overwriting, so a re-run into a non-empty folder silently doubles the files
  instead of replacing them (confirmed by testing) — hence the clear before,
  which also keeps a second uncompressed copy of the database off the disk:
  ```sh
  TS=$(date +%Y-%m-%d)
  STAGING="$BACKUP_HOST_DIR/.staging"
  DEST="$BACKUP_HOST_DIR/influxdb-$TS.tgz"
  mkdir -p "$STAGING"
  find "$STAGING" -mindepth 1 -exec rm -rf {} +
  docker compose exec -T influxdb influx backup "/backups/.staging" \
    --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"
  tar -czf "$DEST" -C "$STAGING" .                # the host write; see below
  find "$STAGING" -mindepth 1 -exec rm -rf {} +
  ```
  Note there is no deletion of anything Drive has a copy of. Deleting is a
  *host* operation and Cloud Sync does see those, so the earlier
  `rm -rf "$DEST"`-then-rewrite shape actively destroyed good cloud backups:
  the delete propagated, the container-written replacement did not. Overwriting
  the archive in place has no such step.
- `influx backup` runs as root inside the container (no `user:` pin in
  `docker-compose.yml`), and the bind mount carries that uid straight through
  to the host, and the archiving `tar` runs as root too, so the archive lands
  `root:root`. The script already runs as root (Task Scheduler), so it re-owns
  the archive to match whoever already owns `BACKUP_HOST_DIR` — the account the
  Drive sync client runs as:
  ```sh
  chown "$(stat -c '%U:%G' "$BACKUP_HOST_DIR")" "$DEST"
  ```
  Worth being precise about what this does and does not fix: it makes the file
  belong to the account the sync client runs as, which is right. It is *not*
  what makes it visible — the old dated folders came out `0755` and were
  world-readable and traversable throughout, so nothing was ever
  permission-blocked. That diagnosis cost two debugging attempts.

#### Making Cloud Sync notice

Synology Cloud Sync does not observe anything written from inside a container
through a bind mount. Not the dated folder, not the files in it, ever — a
folder written that way is still absent from Drive days later, so this is not a
race that a delay could win. Host-side writes to the same tree *are* observed:
the `rm -rf` above propagated a deletion to Drive immediately, and
`backup-grafana.sh` — which writes its `.tgz` from the host — has always
synced. That contrast is what identified the cause.

The first fix attempt assumed a weaker rule than the real one: that **one**
host-written file inside a folder would make Cloud Sync enumerate that folder
and upload everything already in it. Adding a file by hand through File Station
had appeared to do exactly that. So the script's last step wrote one:

```sh
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$DEST/BACKUP-COMPLETE.txt"      # did not work
```

The next night's folder arrived in Drive containing `BACKUP-COMPLETE.txt` and
nothing else, while all fifteen backup files sat on the NAS beside it. That is
the clean experiment, and it gives the actual rule:

> **Cloud Sync uploads the individual writes it observed, and nothing else.**
> There is no folder-level rescan to trigger. (The manual File Station add that
> suggested otherwise went through DSM's own indexing path, not the plain
> filesystem one.)

So the payload itself has to be a host write. `influx backup` still has to run
in the container — it is the container's database — but it writes to
`$BACKUP_HOST_DIR/.staging`, which is never expected to sync, and the host then
writes the one file that is: `influxdb-$TS.tgz`. That is the same shape
`backup-grafana.sh` has always used, and it is the only part of this tree with
an unbroken record of reaching Drive, which is what makes it the design to copy
rather than another theory to test.

Two things follow. Anything added to this tree in future must be written
host-side, or it will sit there unsynced. And restoring is the one place that
rule cuts the other way: unpacking an archive into `.staging` *is* a host
write, so clear it afterwards or a second full copy goes up to Drive.
- Prune local backups older than `BACKUP_RETENTION_DAYS`, operating directly
  on the host path (the script runs on the NAS host, so no need to do this
  inside the container):
  ```sh
  find "$BACKUP_HOST_DIR" -maxdepth 1 -mindepth 1 -type f \
    -name 'influxdb-*.tgz' -mtime +"$BACKUP_RETENTION_DAYS" -exec rm -f {} +
  ```
  A second `-type d -name '20*'` pass ages out the dated folders left over from
  before 2026-08-10; drop it once none remain.
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
  tar -xzf "$BACKUP_HOST_DIR/influxdb-<date>.tgz" -C "$BACKUP_HOST_DIR/.staging"
  docker compose exec influxdb influx restore "/backups/.staging" \
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

1. ✅ `docker compose up -d` after the compose change, bind mount landed.
   (First attempt failed — `.env` on the NAS didn't have `BACKUP_HOST_DIR`
   set yet, so it fell through to the `./backups` default, which didn't
   exist under the repo checkout. Fixed by setting the var and pre-creating
   the Drive-side folder, matching ownership of the parent `googleDrive`
   dir rather than leaving it root-owned from `mkdir`.)
2. ✅ Ran the script by hand; dated folder with InfluxDB backup
   manifest/data files appeared both inside the container
   (`docker compose exec influxdb ls /backups/<today>`) and on the host at
   `/volume1/web/googleDrive/alphaess-influxdb-backups/<today>`.
3. ✅ Checked restore flags against the actual installed CLI
   (`docker compose exec influxdb influx restore --help`) — `--full`,
   `--org`/`-o`, `--token`/`-t` all present as assumed.
4. ✅ Re-ran the script a second time. First attempt: **not** idempotent —
   file count doubled (`influx backup` timestamps rather than overwrites).
   Fixed by clearing the dated folder before each run (§3); re-verified
   after the fix — file count and single timestamp prefix stayed the same
   across repeated runs, and retention pruning left today's fresh folder
   alone.
5. ✅ Added the DSM Task Scheduler entry (daily, `01:00`, no extra
   within-day repeat) and triggered a manual "Run now" — new dated backup
   appeared, file count unchanged (9 files, one timestamp), confirming it
   works under Task Scheduler's environment (minimal `PATH`, `root` user).
6. ✅ Google Drive's own UI showed the dated subfolder under
   `alphaess-influxdb-backups` as `root:root` (parent folder correctly
   `sandeep:users`), confirming it wasn't syncing. Added the `chown` step
   (§3); re-ran on the NAS — fresh dated folder came out owned
   `sandeep:users`, matching the parent. **This step was read as the fix and it
   was not**: files appearing in Drive around the same time came from a manual
   File Station add, not the chown. Backups stopped reaching Drive again
   immediately, and it took until 2026-08-10 to find the real cause.
7. ✅ 2026-08-10: ran the host-archiving script on the NAS;
   `influxdb-2026-08-10.tgz` appeared in Google Drive. First time a backup has
   reached the cloud unaided since this was set up.

   Read the three failed attempts as one lesson: every wrong theory
   (permissions, timing, folder-level rescan) was a guess at *why* Cloud Sync
   behaves as it does, tested by changing the backup. The fix came from
   noticing that something in the same tree already worked —
   `backup-grafana.sh`'s host-written `.tgz` — and copying its shape. When a
   system is opaque, copy the part of it already observed to work rather than
   reasoning about the mechanism.

   Still untested: whether `influx restore` is happy with a backup that has
   been through the tar round-trip. The file set it sees is unchanged, so there
   is no particular reason to expect trouble — but the restore path has not
   been exercised end to end since this changed, and a backup nobody has
   restored is a backup nobody knows works.
