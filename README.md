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

## Notes

- Poll interval floor is 10 s (API rate limit guidance); default is 30 s.
  Check the daily call quota on your open.alphaess.com dashboard.
- On repeated API failures the collector backs off exponentially, capped at
  5 minutes.
- No downsampling: at 30 s intervals a year of data is ~1M points — small
  enough to keep at full resolution forever.
