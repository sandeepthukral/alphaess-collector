# alphaess-collector

Polls an AlphaESS SMILE-G3 system via the [AlphaESS Open API](https://open.alphaess.com/)
every 30 seconds and stores power/SoC samples in InfluxDB, for visualization in Grafana.

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
   password and token (e.g. `openssl rand -hex 32`).
3. Start:

   ```sh
   docker compose up -d --build
   ```

InfluxDB UI is available at http://localhost:8086 (login with
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

## NAS deployment (shared Grafana)

See [DEPLOY.md](DEPLOY.md) — full walkthrough: cloning, secrets transfer,
shared Grafana network setup, starting with the NAS overlay, Grafana
datasource, and first dashboard queries.

## Notes

- Poll interval floor is 10 s (API rate limit guidance); default is 30 s.
  Check the daily call quota on your open.alphaess.com dashboard.
- On repeated API failures the collector backs off exponentially, capped at
  5 minutes.
- No downsampling: at 30 s intervals a year of data is ~1M points — small
  enough to keep at full resolution forever.
