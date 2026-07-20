# alphaess-collector

Polls an AlphaESS SMILE-G3 system via the [AlphaESS Open API](https://open.alphaess.com/)
every 30 seconds and stores power/SoC samples in InfluxDB, for visualization in Grafana.

The default stack is fully self-contained: InfluxDB + collector + a bundled
Grafana with the datasource and dashboard auto-provisioned — `docker compose
up` and you have a working dashboard. If you already run Grafana elsewhere,
see [Using an existing Grafana](#using-an-existing-grafana-nas-deployment).

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

## Using an existing Grafana (NAS deployment)

Alternative to the bundled Grafana: run only InfluxDB + collector and point
an existing Grafana instance (e.g. one shared with other stacks on a NAS) at
this InfluxDB over a shared Docker network. The
`docker-compose.nas.yml` overlay disables the bundled Grafana and joins
InfluxDB to that network.

See [DEPLOY.md](DEPLOY.md) — full walkthrough: cloning, secrets transfer,
shared Grafana network setup, starting with the NAS overlay, Grafana
datasource, and dashboard import.

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
Reordering apps or hiding the time is done in the AWTRIX app settings. The
pusher runs in every deployment mode, since it lives in the base compose file
and the NAS overlays only override Grafana/InfluxDB.

## Notes

- Poll interval floor is 10 s (API rate limit guidance); default is 30 s.
  Check the daily call quota on your open.alphaess.com dashboard.
- On repeated API failures the collector backs off exponentially, capped at
  5 minutes.
- No downsampling: at 30 s intervals a year of data is ~1M points — small
  enough to keep at full resolution forever.
