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

Log timestamps follow `TZ` in `.env` (default `Europe/Amsterdam`). Set it to
the host's zone: left unset, containers run UTC while Grafana and Uptime Kuma
show local time, and the same failure appears at two different clock times.

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

### Nightly conversion-loss update

Back-fills AlphaESS's own metered house load and daily energy totals into
`metered_power` and `daily_energy`, feeding the **AlphaESS Energy Losses**
dashboard. This is what makes the inverter's conversion and standby losses
measurable at all: `power_readings.load_power_w` is the exact identity
`pv + grid + battery`, so its energy balance closes by construction and no loss
can appear in it (see README, "Data collected"). `getOneDayPowerBySn` reports the
same day with a load that *is* metered, and the gap between the two is the loss —
about 1–2.7 kWh a day on this system, against a battery round-trip loss of ~0.7.

Schedule [scripts/daily-efficiency.sh](scripts/daily-efficiency.sh):

- **General**: User = `root` (DSM's docker socket needs root)
- **Schedule**: Daily, first run time `03:00`
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/daily-efficiency.sh
  ```

`03:00`, not just after midnight: AlphaESS finalises a day's totals some minutes
into the next one and returns HTTP 200 with a null or all-zero payload until it
has. `efficiency.py` refuses to store that, so an earlier run would only waste
the window. It is deliberately a separate task from `daily-savings.sh` at 02:00 —
both run under `set -eu`, and a throttled AlphaESS API here would otherwise abort
that script before `pricing.py` runs, coupling a nice-to-have to the job that
produces the money figure.

Run it once by hand first:

```sh
cd /volume1/docker/alphaess-collector
sudo docker compose run --rm collector python efficiency.py --dry-run --date 2026-08-05
```

**No token change is needed.** `INFLUX_TOKEN_COLLECTOR` is already read+write on
`alphaess`, which is where both new measurements live and where `power_readings`
is read from. (The Kuma monitor below does need a new read-only token — see
["Scoped tokens"](#scoped-tokens).)

**One-off backfill.** How far `getOneDayPowerBySn` serves history is unverified,
so do it in chunks of 30 days or fewer and check the first chunk before
committing to a year:

```sh
sudo docker compose run --rm collector python efficiency.py --backfill 2026-07-18 2026-08-05
```

Two API calls per day at `ALPHAESS_MIN_REQUEST_INTERVAL_S` apart, so a 30-day
chunk takes about ten minutes. **That rate budget is shared with the live
collector**, which is polling the same `appId` every 30 seconds from the same
image: a greedy backfill pushes it into its backoff ladder and punches gaps in
`power_readings`, which `pricing.py` then charges against `PRICING_MAX_GAP_S` —
i.e. it can cost whole days of `daily_cost`. Keep the interval at 10 s or more,
run large backfills deliberately, and check `collector_health` afterwards. An
empty response for an old day is logged and skipped, never stored as zero.

**When a day is rejected for `soc_align`.** That gate compares AlphaESS's SoC
against the SoC already in `power_readings` at the same instant, which is the
only available check on the one thing the module has to assume: that
`uploadTime` is stamped in the system's local timezone. A whole-hour error moves
SoC by tens of points on any day the battery cycles. Diagnose with:

```sh
sudo docker compose run --rm collector python efficiency.py --check-alignment --date 2026-08-05
```

It reports the clock lag that best reconciles the two series. `+0.00h` is
correct and is what every day measured so far returns; a whole-hour answer means
the timezone assumption has broken — most likely around a DST transition, so
**2026-10-25 is worth checking by hand.**

And once, after any hardware change:

```sh
sudo docker compose run --rm collector python efficiency.py --system-facts
```

That prints what AlphaESS says the system is and warns if `BATTERY_CAPACITY_KWH`
disagrees with the reported capacity by more than 1%. Worth doing because
`battery_loss_kwh` is directly proportional to a number typed into `.env` by
hand.

## Monitoring the nightly efficiency job

A job that runs once a night and stops is invisible. There is no container to
check — it is a `docker compose run` from Task Scheduler that exits — and every
panel on the Energy Losses dashboard keeps rendering the days it *did* write, so
the only symptom is that the newest bar quietly stops moving.

Four failure modes, and they are not equivalent:

| | Failure | What a naive check sees |
|---|---|---|
| a | The Task Scheduler entry is disabled or removed; the NAS was asleep at 03:00 | nothing runs, nothing complains |
| b | AlphaESS throttles every day in the window | non-zero exit, visible only in DSM's mail |
| c | Every day fails the quality gate | **exits 0 and looks like success** |
| d | Rows land somewhere nothing reads them — token re-scoped, wrong bucket, retention | the job reports success; the dashboard is empty |

Three checks cover them, in the same shape as
["Monitoring that the collector is actually collecting"](#monitoring-that-the-collector-is-actually-collecting).

**1. `EFFICIENCY_HEARTBEAT_URL` (write side, primary).** A second Uptime Kuma
**Push** monitor, separate from the collector's `HEARTBEAT_URL`. Create it with:

- Monitor Type: **Push**
- **Heartbeat Interval**: `86400`
- **Retries**: `1`, **Heartbeat Retry Interval**: `3600`

which trips roughly 26 hours after the last good night — one missed 03:00 run
alerts, a run that slips an hour does not. Paste the URL into
`EFFICIENCY_HEARTBEAT_URL` in `.env`, quoted, for the same `. ./.env` reason as
`HEARTBEAT_URL`.

The job pushes it itself, after a row actually lands — not the shell script on
exit 0, which is precisely what would report case (c) as healthy:

| When | Push | Notification reads |
|---|---|---|
| ≥1 day written | `up` | `OK 2026-08-05: total loss 3.14 kWh (conv 1.79 + bat 1.35); 1 written, 3 skipped` |
| Nothing new, window already full | `up` | `OK (nothing new, up to date through 2026-08-05)` |
| Every attempted day gated | `down` | `GATED 2026-08-05: soc_align 4.2pp > 2.0 (4 days)` |
| Rate-limited out | `down` | `THROTTLED: 3 day(s) lost to rate limiting, newest 2026-08-05` |
| AlphaESS returned nothing | `down` | `NO DATA: AlphaESS returned nothing for 2 day(s)...` |

A quiet night is pushed as `up`, not as silence: without that the monitor would
flap every time the rolling window is already full, and an alert that cries wolf
nightly is one nobody reads. If Python dies before it can push at all, no ping
arrives and the monitor trips on its own — which is what a dead-man's switch is
for, and why the shell script has no trap.

**2. A Kuma monitor that queries InfluxDB directly (read side).** Covers (d) and
anything else where the job is happy but the data is not there, because it reads
the data instead of trusting the writer. Add a second monitor:

- Monitor Type: **HTTP(s) - Keyword**, Keyword: `FRESH`, Heartbeat Interval `3600`
- **Retries**: `1` — at `0` a single blip pages you
- **Heartbeat Retry Interval**: `60` — see
  ["Retries, and why the retry interval is short"](#retries-and-why-the-retry-interval-is-short)
- Method: `POST`, URL: `http://<nas-host>:8086/api/v2/query?org=home`
- **Body Encoding**: leave it on **JSON**
- Headers:

  ```json
  {
    "Authorization": "Token <INFLUX_TOKEN_KUMA>",
    "Content-Type": "application/json",
    "Accept": "application/csv"
  }
  ```

- Body — InfluxDB's JSON query envelope, the Flux carried as a string:

  ```json
  {"query":"from(bucket: \"alphaess\") |> range(start: -14d) |> filter(fn: (r) => r._measurement == \"daily_energy\" and r._field == \"computed_at_unix\") |> max() |> map(fn: (r) => ({_value: if float(v: uint(v: now())) / 1000000000.0 - r._value < 108000.0 then \"FRESH\" else \"STALE\"})) |> yield(name: \"freshness\")","type":"flux"}
  ```

**Why the JSON envelope and not a raw Flux body.** Kuma validates the Body field
against the Body Encoding dropdown, so a raw Flux body pasted under the default
JSON encoding is rejected before the request is ever sent — the API's own
`application/vnd.flux` content type is no help, because the objection is Kuma's
and not InfluxDB's. Sending `application/vnd.flux` with a raw body *does* work
if Body Encoding is switched to **XML**, which skips validation, but that
misdescribes the payload twice over; prefer the envelope above.

Readable form of the same query, for pasting into `influx query`:

```flux
from(bucket: "alphaess")
  |> range(start: -14d)
  |> filter(fn: (r) => r._measurement == "daily_energy"
        and r._field == "computed_at_unix")
  |> max()
  |> map(fn: (r) => ({
      _value: if float(v: uint(v: now())) / 1000000000.0 - r._value < 108000.0
              then "FRESH" else "STALE"
    }))
  |> yield(name: "freshness")
```

No rows at all also fails the keyword match, which is what we want: nothing
written in two weeks is the worst case, not a reason to stay quiet. Paste the
Flux into `influx query` first and confirm it prints `FRESH`, then drop the
`108000.0` to something tiny and confirm it prints `STALE` — a monitor never
seen to fail is not known to work. Do that last check in the monitor itself as
well, not only in `influx query`: it is the only thing that proves the keyword
match and the envelope are both wired up.

### Retries, and why the retry interval is short

Applies to every keyword monitor here. With **Retries** above `0`, a failing
check does not go straight to DOWN — Kuma moves the monitor to **PENDING**
(yellow) and only marks it DOWN (red) once the retries are used up.
Notifications fire on the transition to DOWN and never on PENDING. So the
**Heartbeat Retry Interval** is not a detail: it is how long the monitor sits
yellow and silent before anyone is told. Set it to an hour and a dead job is
known about an hour later than it needed to be — and falsifying the monitor
looks broken, because it goes yellow and appears to stop there.

`60` is right for all of these, and a long value buys nothing. Retries exist
here to absorb a transient failure *reaching InfluxDB* — a dropped connection,
a moment of load — not to smooth the measurement, which cannot be noisy: it is
an elapsed time in hours, and no blip pushes "when did the job last run" across
a 30-hour line. This is the same reasoning the provisioned alert rules give for
`for: 0s`; the metric is inherently debounced, so debouncing it twice only adds
delay. Keeping **Retries** at `1` is what stops a single failed HTTP request
paging you, and that is the part worth having.

**Why `computed_at_unix` and not the row's own timestamp.** `daily_energy` rows
are stamped at the local midnight of the day they *describe*, so the 03:00 run on
day D writes a row stamped D−1 00:00. Just before the next run, the newest row is
already 51 hours old on a perfectly healthy system. A staleness check on that
timestamp would need a threshold above 51 hours and would take two and a half
days to notice a dead job. `computed_at_unix` records when the job actually ran,
so 30 hours catches a single missed night — and a re-run that only confirms old
days still counts as liveness.

**3. Grafana staleness alert + the on-screen stat (read side, secondary).** The
rule in
[`grafana/provisioning/alerting/alphaess-efficiency-staleness.yml`](grafana/provisioning/alerting/alphaess-efficiency-staleness.yml)
fires on the same 30 hours, and on no data at all. It is picked up automatically
by the bundled Grafana's existing `./grafana/provisioning` mount. The same
threshold is rendered as the **Job age** stat in the top row of the Energy Losses
dashboard; a test pins the two together, so the box cannot read green while the
alert fires.

Provisioned rules route through the **default notification policy** — point that
at a real contact point, or all of this fires into nothing.

### Checking the DST fold, each spring and autumn

AlphaESS does not document what timezone `uploadTime` is in. `efficiency.py`
assumes local time and folds the repeated hour accordingly, and the SoC
alignment gate exists to catch a mis-parse — but on ordinary days it reads
**0.00pp**, exactly zero, so lowering its threshold proves nothing. A real DST
transition is the only thing that exercises it.

So on the day after each transition (the last Sunday of March and of October in
`Europe/Amsterdam`), run the dry-run for the transition day itself:

```sh
cd /volume1/docker/alphaess-collector && sudo docker compose run --rm collector \
  python efficiency.py --dry-run --date <the transition day>
```

The metered series is 5-minute, so the record count is the length of the day
times twelve — and that count is the first thing to check, because it is the
cheapest evidence that the fold was handled at all:

| Day | Length | Expect |
|---|---|---|
| Ordinary | 24 h | **288** records |
| Last Sunday in March | 23 h | **276** records |
| Last Sunday in October | 25 h | **300** records |

`soc_align` should stay near zero throughout. If it jumps to tens of pp, the
fold handling is wrong: the day will be *gated* rather than silently
mis-integrated, which is the intended failure — but it needs fixing rather than
waiting out. Diagnose with `--check-alignment` for that date.

Nothing schedules this and nothing alerts on it; it is a twice-yearly manual
check, and the gate is what protects the data in the meantime.

## Monitoring the nightly savings job

`daily-savings.sh` produces the money figure, and it fails the same four ways
the efficiency job does — including the one that exits 0. Left unwatched, a
dead job shows up only as the savings dashboard quietly going flat, which is
easy to mistake for a quiet week.

It has no push heartbeat of its own, so the freshness monitor is the check.
Same shape as the efficiency one above, over `daily_cost`:

- Monitor Type: **HTTP(s) - Keyword**, Keyword: `FRESH`, Heartbeat Interval `3600`
- **Retries**: `1`, **Heartbeat Retry Interval**: `60` — see
  ["Retries, and why the retry interval is short"](#retries-and-why-the-retry-interval-is-short)
- Method: `POST`, URL: `http://<nas-host>:8086/api/v2/query?org=home`
- **Body Encoding**: JSON; headers exactly as above
- Body:

  ```json
  {"query":"from(bucket: \"alphaess\") |> range(start: -14d) |> filter(fn: (r) => r._measurement == \"daily_cost\" and r._field == \"computed_at_unix\") |> max() |> map(fn: (r) => ({_value: if float(v: uint(v: now())) / 1000000000.0 - r._value < 108000.0 then \"FRESH\" else \"STALE\"})) |> yield(name: \"freshness\")","type":"flux"}
  ```

`108000.0` is 30 hours. `daily-savings.sh` runs at 02:00, so one missed night
alerts and an hour's slip does not — the same reasoning as the efficiency job's
threshold, which runs an hour later.

**Deliberately not filtered on `model_version`,** unlike every panel on the
Battery Savings dashboard. `max(computed_at_unix)` across all versions answers
exactly the question being asked — "when did `pricing.py` last write anything"
— and liveness is not a question about a model version. Filtering would make
the monitor read STALE the moment `MODEL_VERSION` is bumped, for as long as the
backfill takes, which is precisely when you are least able to tell a broken job
from an expected gap. This matches the "Job age" stat's exemption on the Energy
Losses dashboard, which `tests/test_model_version_consistency.py` pins in place
for the same reason.

**Deploy ordering.** `daily_cost` rows written before this change carry no
`computed_at_unix`, so `max()` over them returns nothing and the monitor reads
STALE until the first post-deploy run writes the field. Either run the job by
hand straight after deploying:

```sh
sudo docker compose run --rm collector python pricing.py --date <yesterday> --force
```

or create the monitor the following day. Do **not** widen the `-14d` range to
paper over it — that hides exactly the condition the monitor exists to report.

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

It is scheduled through [scripts/backup-all.sh](scripts/backup-all.sh) rather
than directly — see ["Scheduling the backups"](#scheduling-the-backups) below.
Test it on its own first:

```sh
sudo /volume1/docker/alphaess-collector/scripts/backup-influxdb.sh
```

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

## Backing up Grafana

Almost everything in Grafana is provisioned from this repo — the datasource,
every dashboard, and the alert *rules* — so a rebuild from an empty volume
restores them by itself. Two things are not, and they are the ones that fail
quietly:

- **Contact points and the notification policy.** There is no file
  provisioning for them here, so they exist only in `grafana.db` inside the
  `alphaess-grafana-data` volume. Lose that volume and the alert rules
  provision straight back while the routing does not — every rule then fires
  into nothing, which is indistinguishable from healthy. That is the whole
  reason this backup exists.
- **The admin password.** `GF_SECURITY_ADMIN_PASSWORD` binds only at first
  init (see ["Changing the Grafana admin password"](#changing-the-grafana-admin-password)),
  so a restored volume keeps whatever password it was carrying.

Annotations and the installed `volkovlabs-echarts-panel` plugin ride along too.

[scripts/backup-grafana.sh](scripts/backup-grafana.sh) stops Grafana, tars the
volume into `GRAFANA_BACKUP_HOST_DIR/grafana-<date>.tgz`, starts it again, and
prunes archives older than `BACKUP_RETENTION_DAYS`.

**Why it stops Grafana.** `grafana.db` is SQLite, written live, and Grafana
ships no equivalent of `influx backup`. The same argument that rules out
copying InfluxDB's files raw applies here, so the consistency has to come from
there being no writer at all. The stop costs a few seconds in the middle of the
night; a trap restarts Grafana even if the tar or the prune fails, so a bad run
cannot leave it down.

Set `GRAFANA_BACKUP_HOST_DIR` in `.env` to a folder your NAS backup tool
already covers, as with the InfluxDB one. **Do not nest it inside
`BACKUP_HOST_DIR`** — `backup-influxdb.sh` prunes every directory directly
under that path once it ages past `BACKUP_RETENTION_DAYS`, and would delete
this one along with it. A sibling folder is safe.

Scheduled through [scripts/backup-all.sh](scripts/backup-all.sh) alongside the
InfluxDB one — see ["Scheduling the backups"](#scheduling-the-backups) below.
Because this script stops Grafana, run it by hand at least once before it is
ever scheduled, and confirm Grafana comes back:

```sh
sudo /volume1/docker/alphaess-collector/scripts/backup-grafana.sh
sudo docker compose ps grafana
```

### Restoring Grafana

```sh
sudo docker compose stop grafana
VOL=$(sudo docker inspect -f \
  '{{range .Mounts}}{{if eq .Destination "/var/lib/grafana"}}{{.Source}}{{end}}{{end}}' \
  "$(sudo docker compose ps -aq grafana)")
sudo rm -rf "$VOL"/*
sudo tar -xzf "$GRAFANA_BACKUP_HOST_DIR/grafana-<date>.tgz" -C "$VOL"
sudo docker compose start grafana
```

Then check Alerting → Notification policies still delivers to the Telegram
contact point — that is the part the backup exists for, so it is the part
worth verifying.

## Scheduling the backups

One DSM task runs them all. [scripts/backup-all.sh](scripts/backup-all.sh)
calls each `backup-*.sh` in turn and, unlike the individual scripts, does
**not** run under `set -e`: every job runs even if an earlier one fails, so a
broken Grafana backup cannot cost you the database backup. It still exits
non-zero if any job failed, so DSM reports the task as failed and can email
about it.

DSM **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**:

- **General**: User = `root`
- **Schedule**: Daily, first run time `01:00` (ahead of the `02:00`
  battery-savings job)
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/backup-all.sh
  ```

Run each backup on its own first, as described in its section above — this
wrapper only sequences them, so a job that is broken on this NAS is still
broken here.

**Adding a backup later:** write it as its own `scripts/backup-*.sh` and add
the filename to `JOBS` at the top of `backup-all.sh`. Nothing in Task Scheduler
changes. Order in `JOBS` is run order, most important first: a job that *hangs*
(as opposed to failing) blocks the ones after it, so whatever you would most
regret losing goes at the top — which is why InfluxDB is there now.

One failure mode the wrapper does not remove: `backup-grafana.sh` stops Grafana
for a few seconds, and if the task is killed outright in that window Grafana
stays down. Its trap covers `TERM` and `INT`, so DSM's own "stop" is handled;
a `SIGKILL` or a power cut is not. Recovery is
`sudo docker compose start grafana`.

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
| `INFLUX_TOKEN_COLLECTOR` | read + write `alphaess` | Writes `power_readings` and `collector_health`. Read as well, because this image also runs `pricing.py`, which queries `daily_cost` to skip days it has already computed and reads `power_readings`/`market_price` to compute them. The same token covers `efficiency.py` (`metered_power`, `daily_energy`) — no change was needed to add it. |
| `INFLUX_TOKEN_PUSHER` | read `alphaess` | Only ever reads the newest sample. |
| `INFLUX_TOKEN_KUMA` | read `alphaess` | For the Uptime Kuma keyword monitor in ["Monitoring the nightly efficiency job"](#monitoring-the-nightly-efficiency-job). Deliberately not `INFLUX_TOKEN_GRAFANA`: that one also reads `planning`, and pasting it into a second system means revoking one breaks the other. Optional — mint it only if you set that monitor up. |
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

# Only if you set up the Uptime Kuma keyword monitor. This one is pasted into
# Kuma's config, not into .env.
sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "uptime-kuma: r alphaess" \
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

Five of the seven dashboards were exported from the Grafana UI. These two are built by
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

The poll loop catches every exception and backs off (capped at 2 minutes)
instead of exiting, so **container liveness is not a useful signal** — the
process stays up while collecting nothing. Expired credentials, API errors,
InfluxDB write failures and the MTU problem above all look identical from the
outside.

Four checks cover this: three live signals, two of them from opposite ends of
the pipeline and independent of each other, plus a record of what went wrong.

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

The same 5-minute staleness rule is rendered on screen as the **Collector
status** stat, first in the top row of the *AlphaESS Collector Health*
dashboard: a green `ALL OK` or a red `OUTAGE` box answering "is an outage
happening right now", independent of the time picker. It exists because a
Kuma "down" notification says only that pings stopped; this says whether they
have resumed, which is otherwise a matter of noticing the follow-up "up"
notification. A test pins its threshold to the alert's, so the box cannot go
green while the alert fires.

**4. A Kuma monitor that queries InfluxDB directly (read side, independent of
Grafana).** Same shape as the freshness monitor under
["Monitoring the nightly efficiency job"](#monitoring-the-nightly-efficiency-job),
pointed at `power_readings`:

- Monitor Type: **HTTP(s) - Keyword**, Keyword: `FRESH`, Heartbeat Interval `60`
- **Retries**: `1`, **Heartbeat Retry Interval**: `60` — alerts ~2 minutes after
  the data goes stale, matching how quickly check 3 detects it; see
  ["Retries, and why the retry interval is short"](#retries-and-why-the-retry-interval-is-short)
- Method: `POST`, URL: `http://<nas-host>:8086/api/v2/query?org=home`
- **Body Encoding**: JSON; header `Content-Type: application/json`, plus the
  same `Authorization: Token <INFLUX_TOKEN_KUMA>` and `Accept: application/csv`
- Body:

  ```json
  {"query":"from(bucket: \"alphaess\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"power_readings\" and r._field == \"soc_percent\") |> last() |> map(fn: (r) => ({_value: if float(v: int(v: now()) - int(v: r._time)) / 1000000000.0 < 300.0 then \"FRESH\" else \"STALE\"})) |> yield(name: \"freshness\")","type":"flux"}
  ```

The `300.0` is the same five minutes as check 3's threshold, and for the same
reason: comfortably past the collector's 120 s backoff cap, so anything beyond
it means the loop is failing repeatedly rather than merely between polls. Keep
the two in step. A collector dead for over an hour returns no rows at all, so
the keyword does not match and the monitor goes down — the severe case fails
loudly rather than quietly, matching the alert rule's `noDataState: Alerting`.

Note the contrast with the efficiency monitor, which cannot use a row's own
timestamp and needs a `computed_at_unix` field instead: `power_readings` is
written continuously, so here the newest row's timestamp *is* the freshness
signal.

**Why this duplicates check 3.** It does, in what it measures — and not at all
in what it depends on. Check 3 only reaches you if Grafana is running, its
datasource still points at the right InfluxDB, the rule provisioned cleanly,
*and* the default notification policy routes somewhere real. That last one is
easy to get wrong invisibly: an unrouted rule turns red on a dashboard nobody
is looking at and notifies no one, which is indistinguishable from healthy. The
Kuma monitor shares none of that machinery — it talks to InfluxDB directly and
notifies through Kuma's own channel. If you run only one of the live checks,
run the heartbeat (check 1); if you run two, make the second one this.

Provisioned rules route through the **default notification policy**. Point
that at a real contact point (Alerting → Contact points), otherwise check 3
fires into nothing. Checks 1 and 4 are unaffected — they do not go through
Grafana.
