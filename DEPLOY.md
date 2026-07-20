# Deploying with an existing Grafana (NAS)

This is the secondary deployment path, for reusing a Grafana instance that
already runs elsewhere — here, a NAS that runs TeslaMate, which has a Grafana
container in it. (The primary, self-contained path with a bundled,
auto-provisioned Grafana is covered in the [README](README.md).)

Grafana is shared: this stack's InfluxDB joins an external Docker network
that Grafana is also attached to, so Grafana can query it by container name.
The `docker-compose.nas.yml` overlay sets this up and also disables the
bundled Grafana service from the base compose file.

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

Optional: to push live stats to an AWTRIX 3 clock, also set `AWTRIX_HOST` to the
clock's LAN IP (see [AWTRIX clock display](#awtrix-clock-display) below). Leave
it blank to skip that feature — the `awtrix-pusher` service just idles.

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
2. **Name**: `alphaess` (so it's distinguishable from other InfluxDB
   datasources)
3. **Query language**: switch the dropdown from the default *InfluxQL* to
   **Flux**. This is required — InfluxDB 2 with token auth. The form below
   changes when you switch: the InfluxQL-specific *Database/User/Password*
   fields disappear and Flux fields (*Organization/Token/Default Bucket*)
   appear under **InfluxDB Details**.
4. **HTTP → URL**: `http://influxdb:8086` (container port — always 8086 here,
   regardless of any `INFLUX_PORT` host-port remapping in `.env`)
5. **Auth**: leave all toggles off (Basic auth off — auth is via the token
   below)
6. **InfluxDB Details**:
   - **Organization**: `home` (your `INFLUX_ORG`)
   - **Token**: the `INFLUX_TOKEN` value from `.env`
   - **Default Bucket**: `alphaess`
   - **Min time interval**: `30s` (matches the poll interval; avoids Grafana
     requesting finer resolution than the data has)
7. **Save & test** — expect a green "datasource is working" with buckets
   found

> **Why the UI and not provisioning files:** UI-added datasources are stored
> in Grafana's internal database in its Docker volume, which survives
> TeslaMate image updates. TeslaMate's provisioning YAML under
> `/etc/grafana/provisioning/` is baked into its image and gets overwritten
> on every pull — never edit it for this.

> **Warning:** never run `docker compose down -v` on the TeslaMate stack —
> `-v` deletes its volumes, including Grafana's internal database (all
> UI-added datasources and dashboards).

## 8. First dashboard panels

**Shortcut — import instead of building by hand:** the repo ships three ready
dashboards. Import each the same way: Dashboards → **New → Import** → upload the
JSON (or paste its contents) → in the datasource dropdown pick your `alphaess`
datasource → Import. Skip the manual steps below if you use them. (Daily-table
queries pin day boundaries to `Europe/Amsterdam` — edit the `timezone.location`
lines if needed.)

- [grafana/alphaess-dashboard.json](grafana/alphaess-dashboard.json) — the main
  dashboard: all panels below plus daily/hourly energy-total tables. Needs no
  extra plugins.
- [grafana/alphaess-energy-flow.json](grafana/alphaess-energy-flow.json) — the
  **Energy Flow** Sankey, on its own dashboard (defaults to Today; change the
  day with the time picker). This one needs the `volkovlabs-echarts-panel` plugin —
  see [Sankey plugin on the NAS](#sankey-plugin-on-the-nas) below. Without it
  the panel shows a "plugin not found" placeholder; the main dashboard is
  unaffected.
- [grafana/alphaess-battery-savings.json](grafana/alphaess-battery-savings.json)
  — the **Battery Savings** dashboard (euro value of the battery per day). Needs
  no extra plugins, but shows "No data" until the pricing jobs have run — see
  [Battery-savings pricing jobs](#battery-savings-pricing-jobs) below.

### Sankey plugin on the NAS

The **Energy Flow** dashboard (`alphaess-energy-flow.json`) is the one part of
this stack that needs a Grafana plugin (`volkovlabs-echarts-panel`); the main
dashboard needs none. A panel plugin is purely additive —
it registers a new visualization type and touches nothing else (no datasources,
dashboards, or config), so installing it into the shared TeslaMate Grafana is
low-risk. Two ways to provide it:

**Option A — install into the shared Grafana.** One-time, no edits to the
TeslaMate stack's compose:

```sh
docker ps | grep -i grafana        # find the Grafana container name
docker exec <grafana-container> grafana-cli plugins install volkovlabs-echarts-panel
docker restart <grafana-container>
```

> On recent Grafana images `grafana-cli` is not on `PATH` — it was merged into
> the main binary. If you get `"grafana-cli": executable file not found`, use
> `grafana cli` instead:
> `docker exec <grafana-container> grafana cli plugins install volkovlabs-echarts-panel`

The plugin lives in Grafana's data volume, so it survives restarts and
container recreation (as long as that volume persists — if it is ever wiped,
re-run the install, same as the `docker network connect` step above). The one
cost is the restart: a ~10–20s blip on your TeslaMate dashboards.

**Option B — run a dedicated Grafana for this stack.** If you would rather not
touch the TeslaMate container at all (not even a restart), skip the shared
network and run this project's own bundled, auto-provisioned Grafana instead —
it already includes the plugin. Use the dedicated overlay in place of
`docker-compose.nas.yml`:

```sh
docker compose -f docker-compose.yml -f docker-compose.nas-dedicated.yml up -d
# then open http://<nas-host>:3001
```

This publishes Grafana on host port 3001 (override with `GRAFANA_PORT`) so it
does not clash with the TeslaMate Grafana on 3000. The datasource and both
dashboards are provisioned automatically, exactly like the primary README path —
you do
not need steps 4–8 of this guide in that case. Trade-off: a second Grafana
container/login on the NAS, versus keeping everything in one Grafana.

To build manually instead: Dashboards → New dashboard → **Add visualization**
→ select the `alphaess`
datasource, then paste each query below into the raw Flux editor. After
pasting, hit **Refresh** — the panel does not re-run the query on its own.

### Panel 1 — Power overview

All four power series (PV, grid, load, battery):

```flux
from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings")
  |> filter(fn: (r) => r._field == "pv_power_w" or r._field == "grid_power_w" or r._field == "load_power_w" or r._field == "battery_power_w")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
```

Panel settings (right sidebar):

- Panel options → Title: `Power`
- Standard options → Unit: **Watt (W)**
- Legend → Values: `Last`, `Mean` (optional, shows current/average per series)

**Apply** (top right) to return to the dashboard.

### Panel 2 — Solar vs Load vs SoC

Shows how solar production and house load drive battery SoC, plus a computed
`net_power_w = pv − load` series: **negative = house consuming more than the
panels produce**. Mixed units (W and %), so SoC goes on a right-hand axis via
a field override.

```flux
from(bucket: "alphaess")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "power_readings")
  |> filter(fn: (r) => r._field == "soc_percent" or r._field == "pv_power_w" or r._field == "load_power_w")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ r with
      net_power_w: (if exists r.pv_power_w then r.pv_power_w else 0.0)
                 - (if exists r.load_power_w then r.load_power_w else 0.0)
  }))
  |> keep(columns: ["_time", "soc_percent", "pv_power_w", "load_power_w", "net_power_w"])
```

The `pivot` turns the fields into columns so `map` can compute the net series.

Panel settings:

- Panel options → Title: `Solar vs Load vs SoC`
- Standard options → Unit: **Watt (W)** (default for the power series)
- Overrides tab → **Add field override** → *Fields with name* → `soc_percent`:
  - Standard options > Unit → **Percent (0-100)**
  - Standard options > Min → `0`, Max → `100`
  - Axis > Placement → **Right**
- Optional override for `net_power_w`: Graph styles > Fill opacity → ~15, so
  the surplus/deficit area around the zero line stands out
- Apply

### Save

Save icon (top right) → name the dashboard (e.g. `AlphaESS`) → Save. Set the
auto-refresh dropdown (next to Refresh) to `30s`–`1m` for a live view. Panels
move by dragging the title, resize by the corner.

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
docker compose -f docker-compose.yml -f docker-compose.nas.yml \
  run --rm collector python prices.py  --backfill 2026-07-01 2026-07-19
docker compose -f docker-compose.yml -f docker-compose.nas.yml \
  run --rm collector python pricing.py --backfill 2026-07-01 2026-07-19
```

> **Always pass both `-f` files** for `run` too. A bare `docker compose run`
> recreates InfluxDB off the base compose, dropping it from `shared-grafana-net`
> and its `influxdb` network alias — the Grafana datasource then fails with
> `lookup influxdb ... no such host`. If that happens, bring it back with
> `docker compose -f docker-compose.yml -f docker-compose.nas.yml up -d`
> (which re-attaches it _with_ the alias; a manual `docker network connect`
> restores the network but not the alias).

`pricing.py` skips days already written and only stores days with ≥98% sample
coverage, so a range is cheap to re-run and self-heals days skipped for late
prices or gaps. Optionally set `BATTERY_CAPACITY_KWH` in `.env` to also show
each day's SoC change in kWh.

### Nightly battery-savings update

To keep `daily_cost` current, schedule [scripts/daily-savings.sh](scripts/daily-savings.sh)
to run nightly. It cd's into the repo, computes a rolling window (yesterday plus
the 3 days before, TZ-correct), and runs both jobs above — always with both
compose files, so it can't break the datasource.

DSM **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
script**:

- **General**: User = `root` (DSM's docker socket needs root)
- **Schedule**: Daily, first run time `02:00`
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/daily-savings.sh
  ```

Test it once by hand first (`sudo /volume1/docker/alphaess-collector/scripts/daily-savings.sh`)
and confirm the Grafana datasource still resolves afterward. Adjust the window
via `WINDOW_DAYS` near the top of the script.

## Updating

```sh
git pull
docker compose -f docker-compose.yml -f docker-compose.nas.yml up -d --build
```

InfluxDB data lives in the `alphaess-influxdb-data` volume and survives
updates. Only `down -v` on _this_ stack deletes it.
