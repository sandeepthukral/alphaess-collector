# alphaess-collector

Polls an AlphaESS SMILE-G3 system via the [AlphaESS Open API](https://open.alphaess.com/)
every 30 seconds and stores power/SoC samples in InfluxDB, for visualization in Grafana.

The stack is fully self-contained: InfluxDB + collector + a bundled Grafana
with the datasource and dashboards auto-provisioned — `docker compose up` and
you have a working dashboard. A single `docker-compose.yml`, everywhere.
Deploying on a NAS? See [DEPLOY.md](DEPLOY.md).

## Data collected

Measurement `power_readings` in bucket `alphaess` (infinite retention), tagged
with `sys_sn`:

| Field | Unit | Notes |
|---|---|---|
| `pv_power_w` | W | Solar generation |
| `grid_power_w` | W | Positive = import, negative = export (verify with `--once`) |
| `load_power_w` | W | House load |
| `battery_power_w` | W | Positive = discharge, negative = charge (verify with `--once`) |
| `soc_percent` | % | Battery state of charge |

Measurement `collector_health` in the same bucket records why collection
stopped, tagged with `sys_sn`, `event` (`failure` / `recovered`) and — on
failures — `error_class`. Fields: `failures` (consecutive count), `error` (the
message, failures only), `outage_seconds` (`recovered` only). Successful polls
write nothing here: `power_readings` arriving is the healthy signal. Read it
through the **AlphaESS Collector Health** dashboard
([grafana/alphaess-collector-health.json](grafana/alphaess-collector-health.json)).

## Setup

1. Register at [open.alphaess.com](https://open.alphaess.com/), add your system's
   serial number, and note the AppID and AppSecret.
2. `cp .env.example .env` and fill in credentials plus an InfluxDB admin
   password and token (e.g. `openssl rand -hex 32`), and a Grafana admin
   password.
3. Start:

   ```sh
   docker compose up -d --build
   ```

4. Open Grafana at http://localhost:3000 (login with
   `GRAFANA_ADMIN_USER`/`GRAFANA_ADMIN_PASSWORD`). The InfluxDB datasource and
   two dashboards — **AlphaESS** (power/energy overview) and **AlphaESS Energy
   Flow** (a source→use Sankey, defaults to Today) — are provisioned
   automatically, no manual setup. The bundled Grafana also installs the
   `volkovlabs-echarts-panel` plugin the Energy Flow dashboard needs.

   > The dashboard's daily/hourly energy tables pin day boundaries to
   > `Europe/Amsterdam`. If you live elsewhere, edit the `timezone.location`
   > lines in the table panels' queries.

InfluxDB's own UI is also available at http://localhost:8086 (login with
`INFLUX_ADMIN_USER`/`INFLUX_ADMIN_PASSWORD`).

## Verify sign conventions

Before trusting dashboards, check what signs your system actually reports for
grid and battery power:

```sh
docker compose run --rm collector python collector.py --once
```

Prints the raw API response and parsed fields without writing to InfluxDB.
Compare against what the system is doing right now (importing vs exporting,
charging vs discharging).

## NAS deployment

The same self-contained stack runs on a NAS — one `docker-compose.yml`, the
bundled Grafana and all provisioning included. See [DEPLOY.md](DEPLOY.md) for
the NAS specifics: cloning, transferring secrets, host-port conflicts (e.g.
another Grafana already on 3000), verifying the collector's link MTU after
network changes, the nightly battery-savings task, the
[nightly InfluxDB backup](DEPLOY.md#backing-up-influxdb), and collection
monitoring.

## AWTRIX clock display (Ulanzi TC001)

Push a few live stats to an [AWTRIX 3](https://blueforcer.github.io/awtrix3/)
clock (e.g. a modded Ulanzi TC001). The `awtrix-pusher` service reads the most
recent sample **already in InfluxDB** and POSTs it to the clock over HTTP — it
never calls the AlphaESS API, so it adds zero upstream load and is fully
decoupled from the collector.

```
InfluxDB.last() ──(every 30 s)──▶ awtrix-pusher ──HTTP──▶ clock /api/custom
```

Four custom apps rotate in the clock's loop:

| App | Example | Colour |
|---|---|---|
| `soc` | `+85%` / `-85%` | `+` charging, `-` discharging; green→amber→red by level |
| `pv` | `PV 1.8kW` | amber |
| `grid` | `GRID 0.4kW` | green = exporting, red = importing, grey near zero |
| `load` | `LOAD 0.6kW` | blue |

If the newest InfluxDB point is older than `STALE_AFTER_SECONDS` (default 180),
all apps push in dim grey so a dead collector or API outage is visible on the
clock instead of showing silently frozen numbers.

**Icons (optional):** by default the apps are text-only with a short label
(`PV`, `GRID`, `LOAD`). To use AWTRIX's own 8×8 icons instead, upload them via
the clock's web UI **Icons** page (it can fetch by ID from the
[LaMetric icon gallery](https://developer.lametric.com/icons)), then set the
matching env var to that icon's name/ID:

```
AWTRIX_ICON_SOC=1234
AWTRIX_ICON_PV=5678
AWTRIX_ICON_GRID=...
AWTRIX_ICON_LOAD=...
```

When an app has an icon, its text label is dropped (the icon carries the
identity) so the value shows without scrolling — e.g. `☀ 1.8kW`. Colours and
the SoC `+/-` charge indicator still apply.

**Setup:**

1. Reserve a static IP for the clock on your router and set it in `.env`:

   ```
   AWTRIX_HOST=192.168.1.42
   PUSH_INTERVAL_SECONDS=30
   STALE_AFTER_SECONDS=180
   ```

2. Dry-run — prints the fields read and the payloads, then pushes once
   (add `--no-push` to preview without touching the clock):

   ```sh
   docker compose run --rm awtrix-pusher python pusher.py --once
   ```

3. Start it (it also comes up with `docker compose up -d`):

   ```sh
   docker compose up -d awtrix-pusher
   ```

The apps join the clock's rotation automatically — no clock-side config.
Reordering apps or hiding the time is done in the AWTRIX app settings.

## Battery-savings analysis

Quantifies what the battery is worth in euros: for each complete local (NL) day
it prices the real grid flows (Model 1, with battery) against a counterfactual
with no battery (Model 2), using Frank Energie's hourly market prices. The
difference is the battery's value that day. See
[DESIGN-battery-savings.md](DESIGN-battery-savings.md) for the full rationale.

Two batch jobs write to InfluxDB; the **Battery Savings** dashboard
([grafana/alphaess-battery-savings.json](grafana/alphaess-battery-savings.json))
reads the results:

1. `prices.py` — fetches Frank Energie market prices → `market_price` measurement.
2. `pricing.py` — integrates `power_readings` × `market_price` → `daily_cost`.

Neither runs automatically (the collector container only polls live power). Run
them for a range once to backfill, e.g.:

```sh
docker compose run --rm collector python prices.py  --backfill 2026-07-01 2026-07-19
docker compose run --rm collector python pricing.py --backfill 2026-07-01 2026-07-19
```

`pricing.py` skips days already written and only stores days with ≥98% sample
coverage *and* essentially complete prices, so re-running a range is cheap and
self-healing (days skipped for late prices or gaps are retried once the data
lands). Optional: set `BATTERY_CAPACITY_KWH` in `.env` to also express each
day's SoC change in kWh.

### Migrating existing history to model_version 2

The price-coverage gate arrived with `MODEL_VERSION = "2"`. Rows written before
it (version 1) carry no evidence that their prices were complete, so a day that
was priced for only part of its hours is understated there with nothing to show
for it. The arithmetic itself did not change: a fully-priced day computes
identically at 1 and 2.

Recomputing republishes every day that can be verified, at version 2, and
simply omits the ones that cannot. The version-1 rows are left untouched and
become invisible to the dashboard, so nothing has to be deleted:

```sh
# 1. make sure prices are complete for the whole range
docker compose run --rm collector python prices.py  --backfill 2026-07-01 2026-07-27
# 2. recompute. No --force needed: no version-2 row exists yet, so every day
#    is reprocessed.
docker compose run --rm collector python pricing.py --backfill 2026-07-01 2026-07-27
```

A day that appears at version 1 but not version 2 is one whose prices could not
be completed — flip the dashboard's **Model version** variable to `1` to see
what it used to claim.

### Auditing stored days

`--audit` is read-only. It re-checks every stored day at the current
`MODEL_VERSION` against today's gate and reports any whose underlying data has
changed since it was written (prices deleted, samples pruned), printing the
`influx delete` command for each — a rerun cannot fix those, because
`process_day` leaves an excluded day's existing row untouched.

```sh
docker compose run --rm collector python pricing.py --audit
```

To keep it current, schedule [scripts/daily-savings.sh](scripts/daily-savings.sh)
nightly — it reprocesses a rolling window of recent complete days. See
[DEPLOY.md](DEPLOY.md#nightly-battery-savings-update) for the DSM Task Scheduler
setup.

## Development

Tests cover the parts that fail silently: the energy integration and pricing
model, the complete-day quality gate, the collector's failure handling, and the
AWTRIX display formatting. Nothing here needs Docker, InfluxDB, or network
access.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/pytest                                     # tests
.venv/bin/ruff check collector awtrix-pusher tests   # lint
```

Both run in CI on every push and pull request
([.github/workflows/ci.yml](.github/workflows/ci.yml)).

## Notes

- Poll interval floor is 10 s (API rate limit guidance); default is 30 s.
  Check the daily call quota on your open.alphaess.com dashboard.
- On repeated API failures the collector backs off exponentially, capped at
  `MAX_BACKOFF_SECONDS` (default 120 s).

  > **`MAX_BACKOFF_SECONDS` and `PRICING_MAX_GAP_S` are coupled.** The backoff
  > cap sets how long an outage silences the collector, so it sets how big a
  > gap failed polls leave in `power_readings` — and `pricing.py` discards any
  > day whose largest gap exceeds `PRICING_MAX_GAP_S` (1200 s). At 120 s, a run
  > of *k* failed polls leaves `30 + 60 + 120 × (k − 1)` seconds, so eleven
  > consecutive failures still fit under the gate. Raising the cap for API
  > politeness costs whole days of savings data unless you raise the gate too.
  > [tests/test_collector_backoff.py](tests/test_collector_backoff.py) asserts
  > the two stay compatible.
- No downsampling: at 30 s intervals a year of data is ~1M points — small
  enough to keep at full resolution forever.
