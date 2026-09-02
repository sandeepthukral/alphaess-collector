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
| `load_power_w` | W | House load — **derived, not measured**: see below |
| `battery_power_w` | W | Positive = discharge, negative = charge (verify with `--once`) |
| `soc_percent` | % | Battery state of charge |

Measurement `collector_health` in the same bucket records why collection
stopped, tagged with `sys_sn`, `event` (`failure` / `recovered` /
`heartbeat_failed`) and — on failures — `error_class`. `heartbeat_failed` is the
watchdog reporting on itself: a push to Uptime Kuma that could not be delivered,
which leaves the collector healthy and unwatched at the same time. Fields: `failures` (consecutive count), `error` (the
message, failures only), `outage_seconds` (`recovered` only). Successful polls
write nothing here: `power_readings` arriving is the healthy signal. Read it
through the **Collector Health** dashboard
([grafana/alphaess-collector-health.json](grafana/alphaess-collector-health.json)).

> **`load_power_w` is an identity, not a measurement.**
> `load_power_w == pv_power_w + grid_power_w + battery_power_w` holds *exactly*
> on every sample — 56,969 of them checked, maximum residual 0 W — because
> `getLastPowerData` derives house load as the residual rather than metering it.
> Two consequences worth knowing before trusting it: it can go negative (−7847 W
> observed), which no house can do; and the whole-house AC energy balance closes
> by construction, so no conversion or standby loss can ever appear in it. That
> is why the two measurements below exist.

Measurement `metered_power` in the same bucket holds AlphaESS's own 5-minute
history (`getOneDayPowerBySn`), tagged with `sys_sn` and `source`. Written by
`efficiency.py`, once a night for the days just past — not by the live poll loop.

| Field | Unit | Notes |
|---|---|---|
| `metered_load_w` | W | House load, **independently metered** — this is the one that is not the residual |
| `metered_soc_percent` | % | Same quantity as `soc_percent`, by a second path; used to verify the timestamp timezone |
| `feed_in_w` | W | Grid export |
| `grid_charge_w` | W | Grid import |

`ppv` from this endpoint is **deliberately not stored**: it reads `0.0` on every
record on this system — three separate full days summed to 0.00 kWh while the
daily endpoint reported 17–25 kWh of PV for the same days. Stored, it would
render as a night that never ends. PV comes from `pv_kwh_api` below and from
`power_readings.pv_power_w`. Please don't "fix" the missing column.
`pchargingPile` is omitted for the simpler reason that there is no EV charger.

Measurement `daily_energy`, one row per local day, tagged `sys_sn` and
`model_version`, timestamped at local midnight (the same convention as
`daily_cost`). It carries AlphaESS's daily totals verbatim —
`charge_kwh_api`, `discharge_kwh_api`, `pv_kwh_api`, `export_kwh_api`,
`import_kwh_api`, `grid_charge_kwh_api` — plus the loss decomposition computed
from them: `conversion_loss_kwh` (derived load minus metered load),
`battery_loss_kwh` (charge − discharge − ΔSoC×capacity) and `total_loss_kwh`,
alongside `computed_at_unix` and the quality fields the gate ran on. Read it
through the **Energy Losses** dashboard
([grafana/alphaess-energy-losses.json](grafana/alphaess-energy-losses.json)); see
[DEPLOY.md](DEPLOY.md#nightly-conversion-loss-update) to schedule it.

> **Three different export figures now exist and must never meet in one
> expression.** `daily_cost.export_kwh_actual` is this repo's own integration of
> `grid_power_w`; `daily_energy.export_kwh_api` is AlphaESS's daily counter; the
> raw `feed_in_w` series integrates to a third value. On 2026-08-05 the last two
> were 11.55 and 12.28 kWh — a 6% disagreement between two endpoints of the same
> API. That is why every field carries its provenance in its name.

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
   three dashboards — **Overview** (what the system is doing right now: SoC,
   battery power, collector status, a source→use Sankey and the power
   timeseries), **Energy** (the same in totals: per-day stats, bar charts and
   the daily/hourly tables) and **Energy Flow** (a source→use Sankey,
   defaults to Today) — are provisioned automatically, no manual setup. The
   bundled Grafana also installs the `volkovlabs-echarts-panel` plugin the
   Sankey panels need.

   > **Energy**'s daily/hourly energy tables pin day boundaries to
   > `Europe/Amsterdam`. If you live elsewhere, edit the `timezone.location`
   > lines in the table panels' queries.

InfluxDB's own UI is also available at http://localhost:8086 (login with
`INFLUX_ADMIN_USER`/`INFLUX_ADMIN_PASSWORD`).

### Container logs

The **NAS → Container Logs** dashboard shows the logs of every container on the host,
across every compose project, with a saved filter for warnings and errors — `docker
logs` for everything at once. It reads Loki, which is run by the separate
[**nas-observability**](https://github.com/sandeepthukral/nas-observability) stack; this
repo provides the datasource, the dashboard and the error alert rule. A project needs no
logging configuration to appear there. See
[DEPLOY.md, "Container logs"](DEPLOY.md#container-logs).

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
[nightly InfluxDB backup](DEPLOY.md#backing-up-influxdb) and the
[drill that proves it restores](DEPLOY.md#verifying-a-backup-restores), and
collection monitoring.

## Dispatch (writing to the inverter)

Everything above reads. The `dispatch` service is the one that **writes**: it turns
the sibling `battery-planning` project's optimiser output into Modbus commands on the
inverter. It starts in dry run — `DISPATCH_LIVE` defaults to `0`, deciding and
publishing but writing no registers — and going live is a deliberate, separate step.

- Design and the fail-safe: [`DESIGN-dispatch.md`](DESIGN-dispatch.md)
- Going live, once: [`DISPATCH-GOLIVE.md`](DISPATCH-GOLIVE.md)
- **Operating it, including the kill switch:**
  [`DEPLOY.md` → Running the dispatcher](DEPLOY.md#running-the-dispatcher)

The short version of the kill switch, because it should not need a search:
`sudo docker compose stop dispatch` releases the inverter on the way out, and every
command expires within 300 s of the last write regardless — so cutting power or
network to any part of this reverts the battery to self-consumption on its own.

And the short version of "is it actually running", which needs no SSH:

```sh
set -a; . ./.env; set +a && .venv/bin/python3 scripts/is-it-deciding.py
```

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
with no battery (Model 2), using Frank Energie's market prices per billing
slot (hourly through 2026-07-31, 15-minute from 2026-08-01). The
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

`pricing.py` skips days already written and only stores days with ≥96% sample
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

### Migrating existing history to model_version 3

`MODEL_VERSION = "3"` adds `load_kwh` (total house consumption for the day, in
priced hours), which the **Effective €/kWh** panels now use as
`cost_model1 / load_kwh` instead of `saving / avoided-import` — the latter goes
degenerate (near-zero or even negative denominator) whenever the battery's
benefit comes mostly from price arbitrage rather than pure import avoidance.
`cost_model1`, `cost_model2`, `saving`, and the `import`/`export` fields are
unchanged from v2.

Recomputing republishes every day, at version 3, same as the v2 migration:

```sh
docker compose run --rm collector python prices.py  --backfill 2026-07-01 2026-08-02
docker compose run --rm collector python pricing.py --backfill 2026-07-01 2026-08-02
```

The version-2 rows are left in place and simply become invisible once the
dashboard's **Model version** variable is on `3` (already the default).

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

That job stops at yesterday, so it leaves `market_price` with nothing for today
or tomorrow — which the forward-looking **Battery Plan** dashboard needs.
[scripts/refresh-prices.sh](scripts/refresh-prices.sh), scheduled every few
hours, is what keeps that end current; see
[DEPLOY.md](DEPLOY.md#keeping-todays-and-tomorrows-prices-available).

## Public benchmarking (mijnbatterij.nl)

[mijnbatterij.nl](https://mijnbatterij.nl) ranks Dutch home batteries against
each other publicly. The `mijnbatterij` service submits this installation's live
figures every five minutes under the **Doe-het-zelf** control provider — the
battery here is driven by this repo's own dispatcher, not by a supported
aansturingsleverancier.

```
InfluxDB ──(every 5 min)──▶ mijnbatterij.py ──HTTPS──▶ api.mijnbatterij.nl/api/live
  power_readings                                       └▶ mijnbatterij_submit
  market_price                                            (what was sent, and
  daily_cost, daily_energy                                 whether it landed)
```

It reads **only** InfluxDB, so it never touches the AlphaESS API and cannot
perturb collection. Today's euro figure comes from `pricing.compute_day()` — the
same two-world model `daily_cost` stores overnight — run ungated on the partial
day, so the live number and tomorrow's stored one cannot disagree.

Opt-in: with `MIJNBATTERIJ_API_KEY` unset the container idles. The key is issued
on the site's profile page after the installation is registered by hand; there
is no signup API. Look at a payload before publishing anything, because the
first submission is public:

```sh
docker compose build mijnbatterij   # `run` reuses the existing image
docker compose run --rm mijnbatterij python mijnbatterij.py --once --dry-run
```

History goes separately, to `/api/results/daily` (per day) and
`/api/results/monthly` (totals). That is a scheduled job, not only a manual
repair: `scripts/daily-mijnbatterij.sh` runs it nightly at ~03:30, after the
savings and efficiency jobs have written the rows it publishes. Without it the
platform's record of a finished day would be the last live snapshot before
midnight, computed before that day had a `daily_cost` row at all.

```sh
docker compose run --rm mijnbatterij python mijnbatterij.py --monthly 2026-08 --dry-run
docker compose run --rm mijnbatterij python mijnbatterij.py --monthly 2026-08 --test
```

`--test` sets the API's own testing flag: it validates the payload and stores
nothing. The API is specified at
[onbalansmarkt.com/help/api-docs/](https://onbalansmarkt.com/help/api-docs/) —
read it before adding a field.

Full setup, and the two fields that are guesses about an undocumented API
(`batteryPower`'s sign and `mode`), are in
[DEPLOY.md](DEPLOY.md#publishing-to-mijnbatterijnl); the decision flow is in
[docs/MIJNBATTERIJ-FLOW.md](docs/MIJNBATTERIJ-FLOW.md).

## Development

Tests cover the parts that fail silently: the energy integration and pricing
model, the complete-day quality gate, the collector's failure handling, the
AWTRIX display formatting, and the mijnbatterij.nl payload. Nothing here needs
Docker, InfluxDB, or network access.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/pytest                                     # tests
.venv/bin/ruff check collector awtrix-pusher dispatch scripts tests   # lint
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
