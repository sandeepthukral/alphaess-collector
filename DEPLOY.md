# Deploying to the NAS

Deployment of the collector stack to the NAS that already runs TeslaMate,
which has a Grafana container it it. Grafana is shared: this stack's InfluxDB
joins an external Docker network that Grafana is also attached to, so Grafana
can query it by container name.

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
`INFLUX_ADMIN_PASSWORD`, `INFLUX_TOKEN` (generate with `openssl rand -hex 32`).

**Port check:** if another InfluxDB (e.g. Sparky's) already uses host port
8086, set `INFLUX_PORT=8087` (or any free port) in `.env`. This only affects
host access to the InfluxDB UI; Grafana reaches the container over the Docker
network regardless.

## 4. One-time: shared Grafana network

Create the shared network and attach the existing Grafana container to it:

```sh
docker network create shared-grafana-net
docker ps | grep -i grafana        # find the Grafana container name, the last word in the output
docker network connect shared-grafana-net <grafana-container-name>
```

Notes:

- `docker network connect` is live — no Grafana restart needed, and it does
  not touch the TeslaMate stack's own networks or config.
- The connection persists across container restarts, but a `docker compose up`
  that _recreates_ the Grafana container (e.g. after a TeslaMate image update)
  drops it — re-run the `docker network connect` command afterwards. To make
  it permanent instead, add the network to the TeslaMate stack's
  `docker-compose.yml` under the Grafana service:

  ```yaml
  services:
    grafana:
      networks:
        - default
        - shared-grafana-net
  networks:
    shared-grafana-net:
      external: true
  ```

## 5. Start the stack

Always include the NAS overlay file — it attaches InfluxDB to
`shared-grafana-net`:

```sh
docker compose -f docker-compose.yml -f docker-compose.nas.yml up -d --build
```

Check it's collecting:

```sh
docker compose logs -f collector
```

Expected: a `Polling every 30s ...` line and no repeated `Poll failed` errors.

## 6. Verify sign conventions (once)

```sh
docker compose run --rm collector python collector.py --once
```

Confirmed so far (live test 2026-07-17): `pbat` negative = battery charging,
positive = discharging. `pgrid` positive = importing from grid is the expected
convention but was 0 during testing — verify after dark when importing.

## 7. Grafana datasource

In the Grafana UI (not provisioning files):

1. Connections → Data sources → Add data source → **InfluxDB**
2. Query language: **Flux**
3. URL: `http://influxdb:8086`
4. Organization: `home` (or your `INFLUX_ORG`)
5. Token: the `INFLUX_TOKEN` value from `.env`
6. Default bucket: `alphaess`
7. Save & test

> **Why the UI and not provisioning files:** UI-added datasources are stored
> in Grafana's internal database in its Docker volume, which survives
> TeslaMate image updates. TeslaMate's provisioning YAML under
> `/etc/grafana/provisioning/` is baked into its image and gets overwritten
> on every pull — never edit it for this.

> **Warning:** never run `docker compose down -v` on the TeslaMate stack —
> `-v` deletes its volumes, including Grafana's internal database (all
> UI-added datasources and dashboards).

## 8. First dashboard panels

New dashboard querying bucket `alphaess`, measurement `power_readings`.
Example Flux query for the power overview panel:

```flux
from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings")
  |> filter(fn: (r) => r._field == "pv_power_w" or r._field == "grid_power_w" or r._field == "load_power_w" or r._field == "battery_power_w")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
```

Same query with `r._field == "soc_percent"` for a battery SoC panel
(unit: percent, min 0, max 100).

## Updating

```sh
git pull
docker compose -f docker-compose.yml -f docker-compose.nas.yml up -d --build
```

InfluxDB data lives in the `alphaess-influxdb-data` volume and survives
updates. Only `down -v` on _this_ stack deletes it.
