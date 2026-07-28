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
docker compose run --rm collector python prices.py  --backfill 2026-07-01 2026-07-19
docker compose run --rm collector python pricing.py --backfill 2026-07-01 2026-07-19
```

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

## Updating

```sh
git pull
docker compose up -d --build
```

InfluxDB data lives in the `alphaess-influxdb-data` volume and survives updates.
Only `down -v` deletes it.

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

Grafana dashboards from the other project should be provisioned into their own
folder, with their own provider name, mount path, and dashboard UIDs — Grafana
resolves collisions silently, by dropping or overwriting.

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
