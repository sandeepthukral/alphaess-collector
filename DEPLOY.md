# Deploying on a NAS

The same self-contained stack described in the [README](README.md) — InfluxDB +
collector + a bundled, auto-provisioned Grafana — runs unchanged on a NAS. There
is one `docker-compose.yml`; no overlays, no extra `-f` flags. This page covers
the NAS-specific bits: getting the repo and secrets onto the box, host-port
conflicts, verifying the collector's link MTU after network changes, the nightly
battery-savings task, and monitoring that collection is actually happening.

## 1. Clone on the NAS

```sh
git clone https://github.com/sandeepthukral/alphaess-collector.git
cd alphaess-collector
```

## 2. Transfer secrets

Either copy your working `.env` from your machine:

```sh
scp .env <user>@<nas-host>:<path>/alphaess-collector/.env
```

or create it on the NAS and fill it in:

```sh
cp .env.example .env
```

Required values: `ALPHAESS_APP_ID`, `ALPHAESS_APP_SECRET`, `ALPHAESS_SYS_SN`,
`INFLUX_ADMIN_PASSWORD`, `INFLUX_TOKEN` (generate with `openssl rand -hex 32`),
and `GRAFANA_ADMIN_PASSWORD`.

`GRAFANA_ADMIN_PASSWORD` has **no default** — Grafana is the only service
published to the LAN, and its datasource proxy can query every bucket the Grafana
token can read, so a missing key fails loudly instead of coming up on
admin/admin. Same for the three `INFLUX_TOKEN_*` variables below. Compose
interpolates the whole file on *every* subcommand, so until those five keys
exist even `docker compose ps` will refuse, naming the one it wants — that is the
guard working, not a broken checkout.

Optional: to push live stats to an AWTRIX 3 clock, also set `AWTRIX_HOST` to the
clock's LAN IP (see [AWTRIX clock display](#awtrix-clock-display) below). Leave
it blank to skip that feature — the `awtrix-pusher` service just idles.

**Port conflicts.** The stack publishes two host ports; remap either in `.env`
if the NAS already uses it:

- Grafana on `3000` — if another Grafana is already there (e.g. TeslaMate on a
  Synology), set `GRAFANA_PORT=3001` (or any free port).
- InfluxDB on `8086` — if another InfluxDB uses it, set `INFLUX_PORT=8087`. This
  only affects host access to the InfluxDB UI; Grafana reaches InfluxDB over the
  Docker network on the container port regardless.

### Changing the Grafana admin password

`GF_SECURITY_ADMIN_PASSWORD` is applied **only when Grafana first initialises its
database**. Editing `GRAFANA_ADMIN_PASSWORD` in `.env` and restarting does
nothing to an existing install — the old password keeps working and nothing says
so. Reset it through the CLI instead:

```sh
sudo docker compose exec grafana \
  grafana cli --homepath /usr/share/grafana admin reset-admin-password '<new-password>'
```

(On Grafana images older than 10 the binary is `grafana-cli` rather than `grafana
cli`.) Update `.env` to match afterwards, so a rebuild from an empty volume comes
up with the password you actually use.

## 3. Start the stack

```sh
docker compose up -d --build
```

Check it's collecting:

```sh
docker compose logs -f collector
```

Expected: a `Polling every 30s ...` line and no repeated `Poll failed` errors.

Then open Grafana at `http://<nas-host>:3000` (or your `GRAFANA_PORT`). The
InfluxDB datasource and all three dashboards — **AlphaESS**, **AlphaESS Energy
Flow** (Sankey), and **Battery Savings** — are provisioned automatically, and
the bundled Grafana installs the `volkovlabs-echarts-panel` plugin the Sankey
needs. Nothing to configure by hand. (The Battery Savings dashboard shows "No
data" until the pricing jobs have run — see
[Battery-savings pricing jobs](#battery-savings-pricing-jobs) below.)

> The dashboards' daily/hourly energy tables pin day boundaries to
> `Europe/Amsterdam`. If you live elsewhere, edit the `timezone.location` lines
> in the table panels' queries.

### AWTRIX clock display

If you set `AWTRIX_HOST`, the `awtrix-pusher` service comes up with the stack
and pushes SoC / solar / grid / load to the clock every 30 s (reading InfluxDB,
never the AlphaESS API). Dry-run it first:

```sh
docker compose run --rm awtrix-pusher python pusher.py --once
```

Then check the loop:

```sh
docker compose logs -f awtrix-pusher
```

Reserve a static IP for the clock on the router so `AWTRIX_HOST` stays valid.
The container reaches the clock over the NAS's LAN — no extra Docker network
needed. See the [README](README.md#awtrix-clock-display-ulanzi-tc001) for the
app/colour reference and stale-data behaviour.

## 4. Verify sign conventions (once)

```sh
docker compose run --rm collector python collector.py --once
```

Prints the raw API response and parsed fields without writing to InfluxDB.
Confirmed so far (live test 2026-07-17): `pbat` negative = battery charging,
positive = discharging. `pgrid` positive = importing from grid is the expected
convention but was 0 during testing — verify after dark when importing.

## Battery-savings pricing jobs

The **Battery Savings** dashboard reads a `daily_cost` measurement that is _not_
produced by the live collector — two batch jobs populate it (see the
[README](README.md#battery-savings-analysis) and
[DESIGN-battery-savings.md](DESIGN-battery-savings.md) for what they compute):

1. `prices.py` — fetches Frank Energie market prices → `market_price`.
2. `pricing.py` — integrates `power_readings` × `market_price` → `daily_cost`.

Backfill a range once (adjust the start to how far back your `power_readings`
go; end at yesterday — today is incomplete):

```sh
docker compose run --rm collector python prices.py  --reconstruct-if-coarse --backfill 2026-07-01 2026-07-19
docker compose run --rm collector python pricing.py --backfill 2026-07-01 2026-07-19
```

`--reconstruct-if-coarse` matters from 2026-08-01, the 15-minute settlement
cutover. Frank's public API kept returning hourly rows past it, so without the
flag you store hourly prices for a contract billed per quarter; with it, the
quarter-hour shape is rebuilt from EnergyZero's day-ahead feed. It is a no-op
for earlier days and for any row Frank already returns at 15 minutes.

Pass it consistently. Reconstruction writes quarter rows under a different
`source` tag and does not remove the hourly rows for the same span, so a day
fetched both ways ends up holding both — see `CODE-REVIEW.md` #23. Both
scheduled jobs pass it.

`pricing.py` skips days already written and only stores days with ≥98% sample
coverage, so a range is cheap to re-run and self-heals days skipped for late
prices or gaps. Optionally set `BATTERY_CAPACITY_KWH` in `.env` to also show
each day's SoC change in kWh.

### Nightly battery-savings update

To keep `daily_cost` current, schedule [scripts/daily-savings.sh](scripts/daily-savings.sh)
to run nightly. It cd's into the repo, computes a rolling window (yesterday plus
the 3 days before, TZ-correct), and runs both jobs above.

DSM **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**:

- **General**: User = `root` (DSM's docker socket needs root)
- **Schedule**: Daily, first run time `02:00`
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/daily-savings.sh
  ```

Test it once by hand first
(`sudo /volume1/docker/alphaess-collector/scripts/daily-savings.sh`). Adjust the
window via `WINDOW_DAYS` near the top of the script.

That window ends at **yesterday**, and should stay that way: `pricing.py` scores
complete days and today is not one. It is therefore not the job that keeps
today's and tomorrow's prices available — see below.

### Keeping today's and tomorrow's prices available

The **Battery Plan** dashboard draws the raw market price across a `now-6h` →
`now+36h` range. Those are days the nightly job never fetches, so that panel's
price series is empty unless something else keeps the forward end of
`market_price` current.

Schedule [scripts/refresh-prices.sh](scripts/refresh-prices.sh), which runs
`prices.py` with no arguments — yesterday, today and tomorrow:

- **General**: User = `root`
- **Schedule**: Daily, first run time `00:05`, **Repeat every 3 hours**
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/refresh-prices.sh
  ```

It repeats through the day because tomorrow's day-ahead prices are not published
until early afternoon. A day with no prices yet is logged and skipped, not an
error, so runs before publication are simply no-ops for that day. Writes are
idempotent — same slot timestamps overwrite — so the repetition costs nothing
but an API call.

Worth one check after setting it up: an empty price series looks exactly like a
working panel in a quiet market, which is how this went unnoticed from the day
the dashboard was built until 2026-08-02.

## Backing up InfluxDB

InfluxDB's data lives only in the `alphaess-influxdb-data` Docker volume,
which the NAS's Google Drive backup doesn't reach (it only covers ordinary
shared folders). [scripts/backup-influxdb.sh](scripts/backup-influxdb.sh) runs
nightly to land a restorable backup inside a folder the NAS's own backup tool
already watches, so it rides along automatically — see
[BACKUP-DATABASE.MD](BACKUP-DATABASE.MD) for the full design.

It uses `influx backup`/`influx restore` rather than copying the volume
directly, because InfluxDB is a live, continuously-written database and a raw
file copy risks a torn/corrupt snapshot; `influx backup` produces a
consistent, portable backup while the server keeps running.

The script authenticates with the admin `INFLUX_TOKEN`, not a scoped
per-service token — the one deliberate exception to the "Scoped tokens" rule
below. `influx backup`/`influx restore` require operator-level permissions;
scoped/all-access tokens are documented to fail backup with permission
errors, so there's no narrower token available.

Set in `.env`:

- `BACKUP_HOST_DIR` — host directory bind-mounted into the influxdb container
  at `/backups` (see `docker-compose.yml`). Point it at a folder your NAS
  backup tool already covers, e.g.
  `/volume1/web/googleDrive/alphaess-influxdb-backups`.
- `BACKUP_RETENTION_DAYS` — local backups older than this are pruned after
  each run (the external backup target, e.g. Google Drive, keeps its own
  history independently).

DSM **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**:

- **General**: User = `root`
- **Schedule**: Daily, first run time `01:00` (ahead of the `02:00`
  battery-savings job)
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/backup-influxdb.sh
  ```

Test it once by hand first
(`sudo /volume1/docker/alphaess-collector/scripts/backup-influxdb.sh`).

### Restoring

Verify the exact flags for the installed InfluxDB version first — CLI flags
can shift across versions:

```sh
docker compose exec influxdb influx restore --help
```

Then, picking a dated backup folder:

```sh
docker compose exec influxdb influx restore "/backups/<date>" \
  --full --org "$INFLUX_ORG" --token "$INFLUX_TOKEN"
```

## Updating

```sh
git pull
docker compose up -d --build
```

InfluxDB data lives in the `alphaess-influxdb-data` volume and survives updates.
Only `down -v` deletes it.

Three kinds of change are **not** applied by that pair, each silently. If a pull
touched one of them, read the matching section below before assuming you have
deployed it:

| What changed | Why `up -d` misses it | What to run |
|---|---|---|
| `networks:` / `volumes:` | Reused if one already exists under that name | `down` then `up -d` — see below |
| A file under `grafana/provisioning/` | Read at Grafana startup only | `restart grafana` — see below |
| A dashboard's folder or other `dashboards.yml` setting | Checksum unchanged, so the dashboard is skipped | Bump the dashboard JSON `"version"` — see below |

### If the pull changed only `grafana/provisioning/`

`up -d` can be a complete no-op, and it will still report success.

Compose recreates a container only when the service's *resolved* config differs
from the running one. A provisioning file is a bind mount, so editing it changes
nothing Compose compares — and even a `docker-compose.yml` edit is invisible to
that diff when the value it resolves to is unchanged. Dropping the
`GRAFANA_ADMIN_PASSWORD` fallback for a `:?` guard was exactly that: the
password string stayed the same, so `up -d grafana` printed `Container
alphaess-collector-grafana-1  Running` and left the old process in place, with
the new `provisioning/datasources/influxdb.yml` sitting unread inside it.

Read `Running` as "did nothing". `Started` or `Recreated` means it acted.

Grafana re-reads `provisioning/` only at startup, so:

```sh
sudo docker compose restart grafana
```

Datasources and alert rules are upserted on every start, so a restart is enough
for those. Dashboards are not — they are checksum-gated, which is the next
section.

Verify rather than assume, because Grafana logs datasource provisioning below
`info` and a successful restart looks identical to one that changed nothing:

```sh
set -a; . ./.env; set +a
curl -s -u "admin:$GRAFANA_ADMIN_PASSWORD" \
  "http://localhost:${GRAFANA_PORT:-3000}/api/datasources" \
  | grep -o '"name":"[^"]*"\|"isDefault":[a-z]*'
```

A `401` here means `.env` no longer matches the password the running Grafana was
initialised with — see [Changing the Grafana admin
password](#changing-the-grafana-admin-password).

### If the pull changed `networks:` or `volumes:`

`up -d` is **not** enough. Compose reconciles services (it diffs the config and
recreates containers), but it treats networks and volumes as create-if-absent:
when one already exists under that name it is reused and your changed
`driver_opts` / options are ignored, with no warning and no error.
`up -d --force-recreate` does not help either — it recreates the container but
reattaches it to the existing network.

To actually apply such a change, recreate the network:

```sh
docker compose down
docker compose up -d
```

`down` without `-v` keeps the named volumes.

Verify the MTU specifically — a stale `alphaess-net` MTU caused a silent
collection outage on 2026-07-22, because a leftover 1500 MTU only drops the
large TLS handshake packets and shows up as intermittent
`SSL: UNEXPECTED_EOF_WHILE_READING`:

```sh
docker compose exec collector cat /sys/class/net/eth0/mtu   # expect 1400
```

The collector also logs this at startup and warns when it is too high, so
`docker compose logs collector | head` will tell you without the exec. It
re-checks after 3 consecutive poll failures of any kind, as part of the
local-vs-upstream diagnosis below, so a long-running container still reports
it.

## Scoped tokens

`INFLUX_TOKEN` is the admin token. It can create and drop buckets, mint other
tokens, and delete data. Nothing but InfluxDB's own first-start initialisation and
your manual `influx` CLI work should ever hold it.

The nightly backup script is the one exception — see
["Backing up InfluxDB"](#backing-up-influxdb) for why.

Every service gets a token scoped to what it actually does:

| Token | Permissions | Why |
|---|---|---|
| `INFLUX_TOKEN_COLLECTOR` | read + write `alphaess` | Writes `power_readings` and `collector_health`. Read as well, because this image also runs `pricing.py`, which queries `daily_cost` to skip days it has already computed and reads `power_readings`/`market_price` to compute them. |
| `INFLUX_TOKEN_PUSHER` | read `alphaess` | Only ever reads the newest sample. |
| `INFLUX_TOKEN_GRAFANA` | read on every bucket it charts | Read-only. Anyone who reaches the Grafana UI can issue arbitrary Flux through the datasource proxy, so this is the one most worth keeping narrow. |

None of them have a fallback: compose fails to start and names the missing
variable. A service that quietly reverted to the admin token would defeat the
point.

Mint them once, after the stack has started for the first time. `influx auth
create` needs bucket **IDs**, not names:

```sh
cd /volume1/docker/alphaess-collector
set -a; . ./.env; set +a

ALPHAESS_ID=$(sudo docker compose exec -T influxdb influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name "$INFLUX_BUCKET" --hide-headers | awk '{print $1}')
echo "alphaess bucket id: $ALPHAESS_ID"

sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "collector: rw alphaess" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$ALPHAESS_ID"

sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "awtrix-pusher: r alphaess" \
  --read-bucket "$ALPHAESS_ID"
```

Copy each printed token into the matching `.env` variable, then `sudo docker
compose up -d`. The Grafana token is created the same way but should list a
`--read-bucket` for every bucket it charts — see below.

> Tokens are shown in full only when created. If you lose one, delete it
> (`influx auth list` / `influx auth delete`) and mint a replacement; there is no
> way to read it back.

## Sharing the stack with another project

Another compose project on the same NAS can use this InfluxDB and Grafana rather
than running its own.

**This repository owns InfluxDB, Grafana, their volumes, and `alphaess-net`.**
The other project declares all of them external and defines none of its own. Two
compose files each defining an `influxdb` service on one host does not announce
itself as a mistake — it just produces two databases, two volumes, and half the
data in each.

On the other side:

```yaml
services:
  your-service:
    environment:
      # The service name on the shared network. NOT http://<nas-ip>:8086 --
      # that works, which is the problem: it routes over the LAN and puts the
      # token in cleartext on every write, in a .env nobody revisits.
      INFLUX_URL: http://influxdb:8086
    restart: unless-stopped   # depends_on cannot reach another compose project
    networks:
      - alphaess-net

networks:
  alphaess-net:
    external: true
```

Three things worth knowing before you write that file:

- **Joining `alphaess-net` is what confers the 1400 MTU cap.** A container that
  creates its own network gets the default 1500 and inherits the TLS problem
  described above from scratch — intermittent
  `SSL: UNEXPECTED_EOF_WHILE_READING` against whatever external HTTPS API it
  calls, days apart, with nothing pointing at the network.

- **Give it its own bucket and a token scoped to that bucket.** Not
  `INFLUX_TOKEN` — that is the admin token, and it can drop the `alphaess`
  bucket and all of its history. Retention is a per-bucket property, so a
  project that needs a different retention than `alphaess`'s infinite one has no
  alternative to its own bucket.

### The `planning` bucket

The first such project. 400-day retention, measurement `plan`, tag `plan_run`,
~770 points/day.

```sh
set -a; . ./.env; set +a

sudo docker compose exec -T influxdb influx bucket create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -n planning -r 400d

PLANNING_ID=$(sudo docker compose exec -T influxdb influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name planning --hide-headers | awk '{print $1}')

# For the planning project itself: read alphaess, write planning.
sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "planning: r alphaess, w planning" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$PLANNING_ID"

# For Grafana: read-only on both, so Flux can join across them.
sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "grafana: r alphaess, r planning" \
  --read-bucket "$ALPHAESS_ID" --read-bucket "$PLANNING_ID"
```

The second token goes in `INFLUX_TOKEN_GRAFANA` here; the first goes in the
planning project's own `.env` and never appears in this repository.

> **The planning token cannot read `planning`.** That is deliberate, but it means
> the project cannot query back what it has written — no idempotent "skip runs
> already computed", no audit of its own rows. `pricing.py` in this repo relies on
> exactly that pattern. If the planning code needs it, add read:
>
> ```sh
> sudo docker compose exec -T influxdb influx auth create \
>   -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "planning: r alphaess, rw planning" \
>   --read-bucket "$ALPHAESS_ID" --read-bucket "$PLANNING_ID" --write-bucket "$PLANNING_ID"
> ```

**Retention and cardinality.** `plan_run` grows without bound, which is normally
an InfluxDB anti-pattern — every distinct tag value is a new series. The 400-day
retention is what makes it safe: InfluxDB drops whole expired shard groups and
their series leave the index with them, so cardinality settles at roughly
`runs_per_day × 400 × series_per_run` rather than growing forever. At ~770
points/day this stays small. Revisit if `plan_run` ever produces more than a few
thousand retained values.

Two details worth knowing: a 400-day retention uses 7-day shard groups, so data
is removed up to a week after it expires rather than to the day; and retention
cannot be enforced per measurement, only per bucket — anything written to
`planning` inherits the 400 days.

- **`docker compose down` here stops the shared services** out from under the
  other project, which keeps running and failing. `down -v` destroys both
  projects' data. Note that the MTU procedure above prescribes exactly that
  `down`/`up` cycle.

### Grafana provisioning: one prefix per project

Grafana resolves provisioning collisions **silently** — by dropping a provider,
overwriting a dashboard, or picking one of two definitions by file load order.
Nothing fails, nothing is logged; a dashboard just vanishes or charts the other
project's data. So every identifier is namespaced by project. This repo uses
`alphaess` throughout; the other project must pick its own prefix and use it for
all of these:

| Identifier | Here | If both projects use the same value |
|---|---|---|
| Datasource `name` / `uid` | `alphaess` | One definition wins, and which one depends on file load order. |
| Dashboard provider `name` | `alphaess` | Provider names must be unique — Grafana drops one, and its dashboards never appear. |
| Provider `options.path` | `/var/lib/grafana/dashboards` | Both providers claim the same files, and each deletes the dashboards it does not recognise. Mount somewhere else, e.g. `/var/lib/grafana/dashboards-<prefix>`. |
| Dashboard `folder` | `AlphaESS` | Not harmful, but the point of the folder is telling the two apart. |
| Dashboard `uid`s | `grafana/*.json` | A duplicate uid overwrites the other project's dashboard. |
| Alert group `name` / rule `uid` | `alphaess-health` / `alphaess-data-stale` | The same rule uid replaces the rule; the displaced alert stops evaluating. |

**No datasource sets `isDefault`.** Removed here on purpose: two datasources both
claiming the default is the one collision with no visible symptom at all, since
Grafana picks one and any panel that relies on "default" then charts the wrong
database with plausible-looking numbers. Every panel must name its datasource —
in this repo they all reference `${DS_ALPHAESS}` and the staleness rule names
`datasourceUid: alphaess`, so nothing depended on the default. The other project
should not set it either; if it does, a panel here that someone creates in the UI
and forgets to point at `alphaess` will read *its* bucket.

### If the pull changed the dashboard folder

Same shape of trap as `networks:` above, and just as quiet: `up -d` reprovisions,
but **existing dashboards do not move**.

Grafana skips a provisioned dashboard whose checksum has not changed, and the
Grafana entrypoint rewrites the JSON byte-identically on every start — so the
files look untouched, the update is skipped, and the folder in
`provisioning/dashboards/dashboards.yml` is never applied to dashboards that are
already there. A fresh install lands in the right folder. An upgrade does not,
and logs nothing to explain it.

**Deleting them is not the answer** — Grafana refuses (*"provisioned dashboard
cannot be deleted"*), from the UI and the API alike, and no setting changes that.
`disableDeletion` governs whether *provisioning* removes a dashboard when its file
disappears; it does not grant you permission to delete one by hand.

Change the file instead. Bump the top-level `"version"` in each dashboard JSON:

```sh
grep -n '^  "version":' grafana/*.json
```

That is enough — the checksum is over the file's bytes, so any change makes the
provisioner stop skipping it, and a dashboard it actually processes is written
into the provider's current folder. Commit the bump so every deployment converges
on the same state, then:

```sh
sudo docker compose restart grafana
sudo docker compose logs grafana 2>&1 | grep -i 'provision' | tail
```

> `allowUiUpdates: true` means a dashboard saved from the Grafana UI has drifted
> from the file on disk. Re-provisioning overwrites it from the file, so export
> anything you edited in the UI and want to keep **before** restarting.

The same trick applies to any provisioned dashboard change that Grafana appears to
ignore, not just folders.

### The two battery-plan dashboards are generated, not exported

Four of the six dashboards were exported from the Grafana UI. These two are built by
scripts, because their Flux queries were written and checked against the live database
first, and those queries are the substance of the dashboard rather than its layout:

| script | dashboard | reads | looks |
|---|---|---|---|
| [`grafana/generate-battery-plan.py`](grafana/generate-battery-plan.py) | `alphaess-battery-plan.json` | `plan` | forward, `now-6h` to `now+36h` |
| [`grafana/generate-battery-score.py`](grafana/generate-battery-score.py) | `alphaess-battery-score.json` | `plan_score` | back, over finished days |

Both read the `planning` bucket, which the **battery-planning** repo writes: the planner
writes `plan` every three hours, and `report_day.py` writes `plan_score` at 06:10 for the
day that just ended. Neither is written by anything in this repo, so an empty dashboard
here usually means a job did not run over there — the "Plan age" and "Score age" stats
exist to say which.

Edit the script, then regenerate:

```sh
python grafana/generate-battery-plan.py grafana/alphaess-battery-plan.json
python grafana/generate-battery-score.py grafana/alphaess-battery-score.json
```

`tests/test_grafana_provisioning.py` re-runs each generator and compares, so a hand-edit
to the JSON fails the suite. The pairing is by filename — `generate-X.py` must emit
`alphaess-X.json` — so a third generated dashboard is covered automatically. Bump
`"version"` in the script, not in the output.

The UI is still the right place to try a change; it just has to come back to the script,
since re-provisioning overwrites the UI copy from the file.

## Monitoring that the collector is actually collecting

The poll loop catches every exception and backs off (capped at 5 minutes)
instead of exiting, so **container liveness is not a useful signal** — the
process stays up while collecting nothing. Expired credentials, API errors,
InfluxDB write failures and the MTU problem above all look identical from the
outside.

Three checks cover this: two live signals from opposite ends of the
pipeline, plus a record of what went wrong.

**1. `HEARTBEAT_URL` (write side, primary).** Set it to an Uptime Kuma
**Push** monitor URL and the collector pings it after each successful
InfluxDB write — a dead-man's switch over the whole collect→write path. Set
the Kuma monitor's grace period above `POLL_INTERVAL_SECONDS`; allow for the
5-minute backoff cap, so ~10 minutes is a sensible floor or you will get
false alarms on a transient blip.

The pings carry a status and a message, so the alert explains itself:

| When | Push | Notification reads |
|---|---|---|
| Successful poll | `status=up&msg=OK` | — |
| 2nd+ consecutive failure | `status=down` + the error | `ReadTimeout: HTTPSConnectionPool(host='openapi.alphaess.com'...): Read timed out. (read timeout=30)` |
| 3rd+ consecutive failure | the error + a verdict | `SSLError: SSLEOFError(8, '[SSL: UNEXPECTED_EOF...' (3 consecutive failures) [upstream]` |
| First poll after an outage | `status=up` + duration | `OK (recovered after 5 failures, 12m11s)` |

The first failure never pushes `down` — a single failed poll is usually an
upstream blip the next poll rides out, and paging on it means being woken for
something already fixed. From the second onwards the grace period would expire
anyway, so this only changes *what the alert says*, not when it fires.

That matters because the failure modes are not equivalent and the phone should
say which one you have. The exception alone does not settle it — a TLS EOF is
the signature of an oversized container MTU *and* of an upstream edge dropping
connections — so on the 3rd consecutive failure the collector probes DNS for
the API host and one unrelated HTTPS endpoint (`DIAGNOSTIC_URL`, an IP so it
does not depend on DNS) and logs a verdict, which is appended to the alert:

| Verdict | Means | What to do |
|---|---|---|
| `[upstream]` | DNS and unrelated HTTPS both fine | Nothing — AlphaESS is down, the collector resumes on its own |
| `[local-network]` | Unrelated HTTPS fails too | Check the uplink; if only TLS fails, check the link MTU in the same log block |
| `[local-dns]` | API host does not resolve | Check the container's DNS and the host's uplink |

The probe runs once per outage, not per failure: the answer cannot change
while the same run of failures continues. Without any of this, both cases read
as "no ping received" and cost a trip to the logs.

Log volume during an outage is bounded the same way: the first failure logs a
full traceback, subsequent ones log a single line with the error. A 15-minute
outage is ~10 readable lines rather than several hundred frames of identical
`requests`/`urllib3` stack, and `Poll recovered after N consecutive failures
(12m11s)` marks where it ended.

**2. `collector_health` in InfluxDB (history, after the fact).** Every failed
poll and every recovery is written to a `collector_health` measurement in the
same bucket, tagged `event` (`failure`/`recovered`) and `error_class`. InfluxDB
is local, so it keeps accepting writes precisely when the AlphaESS API is
unreachable — it records the outage while it is happening. The
**AlphaESS Collector Health** dashboard
([`grafana/alphaess-collector-health.json`](grafana/alphaess-collector-health.json))
reads it: failed polls, outage count and duration, failures split by error
class, and a table of the actual error messages. It is provisioned
automatically (bind-mounted into `dashboard-src` like the other dashboards).
That table is the answer to
"what did the alert mean", reachable from a phone instead of
`docker compose logs`. Writes are best-effort and never fail a poll; if
InfluxDB itself is the thing that is broken, nothing is recorded (the
heartbeat still fires, which is the point of having both).

**3. Grafana staleness alert (read side, secondary).** The rule in
[`grafana/provisioning/alerting/alphaess-staleness.yml`](grafana/provisioning/alerting/alphaess-staleness.yml)
fires when the newest `power_readings` sample is more than 5 minutes old, and
on no data at all. The bundled Grafana mounts `./grafana/provisioning`, so the
rule is picked up automatically — nothing to install.

The two live checks overlap substantially — the heartbeat already catches most
stalls. The staleness alert adds the cases the heartbeat structurally cannot see, because
it queries the data rather than trusting the writer: a wrong bucket, a
retention policy quietly dropping data, a Grafana datasource pointed
elsewhere, or `HEARTBEAT_URL` simply never being set. If you run only one, run
the heartbeat.

Provisioned rules route through the **default notification policy**. Point
that at a real contact point (Alerting → Contact points), otherwise the alert
fires into nothing.
