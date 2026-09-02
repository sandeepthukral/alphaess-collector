# Deploying on a NAS

The same self-contained stack described in the [README](README.md) — InfluxDB +
collector + a bundled, auto-provisioned Grafana — runs unchanged on a NAS. There
is one `docker-compose.yml`; no overlays, no extra `-f` flags. This page covers
the NAS-specific bits: getting the repo and secrets onto the box, host-port
conflicts, verifying the collector's link MTU after network changes, the nightly
battery-savings task, and monitoring that collection is actually happening.

**Stopping the dispatcher in a hurry:** `sudo docker compose stop dispatch`. It
releases the inverter on the way out, and anything it wrote expires within 300 s
regardless — see [Running the dispatcher](#running-the-dispatcher).

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
admin/admin. Same for the four `INFLUX_TOKEN_*` variables below. Compose
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
InfluxDB datasource and all three dashboards — **Overview**, **Energy Flow**
(Sankey), and **Battery Savings** — are provisioned automatically, and
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

## Running the dispatcher

Every other service here reads. The dispatcher **writes to hardware**, so it is the
one that needs a stop procedure you can follow without thinking. Going live is a
separate, deliberate step — `DISPATCH-GOLIVE.md` — but once it is live, this is how
you operate it. The design reasoning behind all of it is `DESIGN-dispatch.md` §8.

### Is it deciding? — the check that runs from your laptop

```sh
set -a; . ./.env; set +a          # for INFLUX_TOKEN_GRAFANA, read-only
.venv/bin/python3 scripts/is-it-deciding.py
```

```
  decision   hold       battery frozen at 0 W -- Mode 3, surplus exported
  readback   hold (battery frozen) / 0 W (live)
  plan       2026-08-18T20:05:02Z  (1.9 h old)
  last tick  23:58:17  (4 s ago)

DECIDING -- ticking every 60 s as it should.
```

No SSH and no Modbus — it reads InfluxDB over the tailnet, so it works from
anywhere and cannot perturb the thing it is measuring. Exit code is `0` deciding,
`1` stalled, `2` down, so it drops straight into a shell check. `--watch` re-reads
every 60 s, which is how you catch a decision changing at a slot boundary.

**It is honest about absence, which the dashboard is not.** The query windows five
minutes, so a dispatcher that died during breakfast returns no rows and prints
`NOT DECIDING` — rather than the confident, well-formatted answer a wider range with
`last()` would give you about a decision made hours ago. Nothing else here has that
property for free.

Two readings that look like faults and are not:

- **`decision (no slot)`** — `slot_action` is only written while a slot is active, so
  a healthy dispatcher outside the plan's horizon, or in a gap between slots, writes
  a point without it. Normal resting state.
- **`readback ... (dry run)`** — dry run writes no registers, so the readback never
  changes whatever the dispatcher decided. In dry run the `decision` line is the only
  one carrying information, which is the entire reason it is published from the slot
  rather than from the registers.

#### The three checks, and when each is the right one

| | Reads | Needs | Answers |
|---|---|---|---|
| `scripts/is-it-deciding.py` | InfluxDB | the tailnet | **Right now.** "Did I just break it" |
| `scheduler.py --alive` | the heartbeat file in the container | `sudo docker compose exec` on the NAS | The loop is alive without opening a Modbus connection. This is also the Dockerfile's healthcheck |
| `scripts/review-dry-run.py` | InfluxDB, a whole day | the tailnet | **"Was yesterday any good."** The only one that sees gaps: a loop that died and restarted looks perfectly healthy to the other two a minute later |

`TICK_S` and `GAP_S` are deliberately the same numbers in the first and third, and
must stay in step with Kuma monitor #5's interval — three components disagreeing
about what counts as a stalled loop is worse than any one of them being wrong.

**On the dashboard, the panel that cannot lie is `Command expires in`.** `Dispatch
state` reading `hold (battery frozen)` is *not* evidence the loop is running: a
released dispatch and a dead dispatcher have identical registers (`start=0`), so only
the freshness of the point separates them. `Command expires in` is gated on
`expires_at` and `dispatch_active` being written at the same instant and floors at
zero, so a stopped loop drains it to red rather than leaving it sitting at a healthy
five minutes.

### The kill switch

```sh
sudo docker compose stop dispatch
```

That is the whole thing. SIGTERM reaches the scheduler (`entrypoint.sh` execs it as
PID 1 precisely so it does), the loop breaks, and `finally:` writes `REG_START = 0`.
The inverter is back on plain self-consumption before the command returns —
**0.4 s**, measured on the first live stop, 2026-08-18.

**If that fails, silence still works.** Every command carries
`DISPATCH_DURATION_S = 300`: nothing this dispatcher writes survives five minutes
without being actively refreshed. Kill the container, kill the NAS, unplug the
switch the inverter is on — it reverts on its own. There is no state you can reach
where a bad command outlives five minutes of nothing happening, which is why "stop
writing" is always a valid emergency response and why `stop_grace_period: 30s`
(below) is a convenience rather than a safety property.

### Reading the last lines of the log

```sh
sudo docker compose logs --tail 5 dispatch
```

One line per 60 s tick, and every field earns its place:

```
command | hold at 0 W | soc=4.8% | temp=18.4/23.7C | verified=True | ours
```

| Field | Reads | Means |
|---|---|---|
| kind | `command` / `release` / `idle` | `idle` is the fail-safe: no plan, stale plan, no slot, or SoC unreadable. It writes nothing after releasing once |
| reason | `hold at 0 W` | The decision in words, including a downgrade — a charge whose target is not above live SoC says so here and becomes a hold |
| soc | `4.8%` | Live SoC read this tick. `?` means the register could not be read, and no Mode 2 command is issued when it says that |
| temp | `18.4/23.7C` | Coldest and hottest cell in the whole battery — min/max across all three packs, not one reading per pack. `?` means the block could not be read, or decoded to a temperature `registers.temps_plausible` refused to vouch for |
| verified | `True` / `False` / `None` | The post-write readback matched what was written. **`False` is normal in dry run** and means nothing — nothing was written, so nothing can verify. Live, `False` is monitor #6's alarm: the log says commanded, the battery is not |
| ownership | `ours` / `HIJACKED` | `HIJACKED` means the block is active with a command this process did not write, i.e. the app is driving. §8 |

After a stop, the line you want is:

```
dispatch released on exit
```

That is `finally:` confirming the release actually wrote, as distinct from the
container merely dying. **Its absence after a stop is the one result worth acting
on** — see below.

`tick failed, continuing:` with a pymodbus traceback is the inverter not answering,
not a dispatcher fault. Two or three in a row is a normal transient; the loop keeps
its 60 s cadence and publishes a degraded `dispatch_state` point carrying the
decision and `read_error`, so the gap shows up as an event with a cause rather than
a hole in the series.

### Starting it again

```sh
sudo docker compose up -d dispatch
```

**Named service, always.** A bare `docker compose up -d` recreates the collector and
cost 922 s of samples on 2026-08-10.

It re-commands within one tick. Add `--build` only when the change touched
`dispatch/` source; a `docker-compose.yml` or `.env` change needs no rebuild.

**Expect the dashboard to be wrong for a few minutes after a stop.** `Dispatch state`
will still read `hold (battery frozen)` and `0x0880` will still show `1`, because the
publish happens inside `tick()` while the release runs in `finally:` after the loop —
so the last point in Influx is the last *commanded* state, not the released one. The
inverter is genuinely released; the series has not caught up. `Command expires`
counting down to 0, and monitor #5 `dispatcher-alive` going red after 180 s, are the
panels telling the truth in that window.

### If the release did not land

You stopped it and there is no `dispatch released on exit`. Nothing is on fire: the
dead man's switch has it, and the inverter reverts within 300 s of the last write.
Watch `Command expires` reach 0 and `0x0880` fall to `0` on the dashboard.

Then find out why, because it should not happen. `stop_grace_period: 30s` on the
dispatch service exists for exactly this: the release is a Modbus write, and against
an unresponsive inverter pymodbus spends 3 s × 3 retries — about 12 s — before
giving up, which is longer than Docker's default 10 s grace. A stop that took the
full 30 s and still did not release means the inverter was not answering at all, and
the next question is whether anything else is holding its single Modbus connection.

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

`pricing.py` skips days already written and only stores days with ≥96% sample
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
`metered_power` and `daily_energy`, feeding the **Energy Losses**
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
alerts, a run that slips an hour does not.

**`3600` is deliberate here, and this is the one monitor it belongs on.**
["Retries, and why the retry interval is short"](#retries-and-why-the-retry-interval-is-short)
argues for `60` everywhere else; that argument is about the keyword monitors,
whose measurement is an elapsed time in hours read against a 30-hour line. A
Push monitor has no such margin built into the check itself — the heartbeat
interval *is* the deadline, so the slack has to come from the retry interval.
Set this one to `60` and the monitor goes down 24 hours and one minute after
the last push, i.e. any night the job starts a few minutes late. Paste the URL into
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

`60` is right for all of these — but only these; the Push monitor above wants
`3600` for a reason that does not apply to a keyword check. For the keyword
monitors a long value buys nothing. Retries exist
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

## Publishing to mijnbatterij.nl

[mijnbatterij.nl](https://mijnbatterij.nl) benchmarks Dutch home batteries
against each other publicly. The `mijnbatterij` service submits this
installation's live figures to it every five minutes, reading **only** InfluxDB
— `power_readings`, `market_price`, `daily_cost`, `daily_energy` — so it never
touches the AlphaESS API and cannot perturb collection.

It runs from the collector image with a different command, and it is **opt-in**:
with `MIJNBATTERIJ_API_KEY` unset the container idles instead of crash-looping,
the same shape as `awtrix-pusher` without `AWTRIX_HOST`.

### 1. Register the installation (browser, once)

There is no signup API. On the site, create an account and add an installation:

| Field | Value |
|---|---|
| Merk / model | AlphaESS SMILE-G3-S5 |
| Opslagcapaciteit | `BATTERY_CAPACITY_KWH` from `.env` (27.9 kWh) |
| Max vermogen | 5 kW |
| Netaansluiting | your fuse rating, e.g. `3x25A` |
| Aansturing | your energy supplier — **Frank Energie** here. This is *who supplies the battery's control*, not how it is driven |
| Modus | **Handmatig/doe-het-zelf** — this is the field that says the dispatching is your own |
| Locatie, gebruikersnaam | shown publicly — pick accordingly |

The API key then appears on the profile page. It is a credential for an
outbound *public* submission, so it goes in `.env` and nowhere else:

```
MIJNBATTERIJ_API_KEY=<key from the profile page>
MIJNBATTERIJ_CYCLES_OFFSET=0
```

Mint `INFLUX_TOKEN_MIJNBATTERIJ` with the rest — see
["Scoped tokens"](#scoped-tokens). Unlike the dispatcher's token this one is
**not** `:?`-guarded, so it needs no placeholder first: pulling this branch
leaves every existing compose command working whether or not you ever set it.
The service refuses to start on an empty token once `MIJNBATTERIJ_API_KEY` is
filled in, naming the variable.

### 2. Check the payload before publishing anything

The first submission is public. Look at it first, from the Mac or the NAS:

```sh
sudo docker compose build mijnbatterij   # `run` never builds; see below
sudo docker compose run --rm mijnbatterij python mijnbatterij.py --once --dry-run
```

That prints the exact body and posts nothing. Three numbers are worth reading
carefully, because nothing downstream can catch them being wrong:

- **`batteryPower` sign.** AlphaESS's `pbat` is positive while *discharging*;
  this sends charge-positive. The convention the platform expects is not
  documented anywhere public. If its graph reads inverted, set
  `MIJNBATTERIJ_CHARGE_POSITIVE=0` — do not change the sign in the code, or the
  payload silently disagrees with `power_readings`.
- **`totalBatteryCycles`.** Computed as total kWh discharged (from
  `daily_energy`, at the current `model_version`) over usable capacity, so it
  counts only what this collector has seen. Days `daily_energy` has no row for —
  not yet written, never served, or gated — are integrated out of
  `power_readings` instead, so the counter cannot step backwards; the log names
  each one it fills.

  **`MIJNBATTERIJ_CYCLES_OFFSET` is what the pack did BEFORE that record
  begins, and nothing else.** It is added on top of everything measured, so
  putting the figure the dry run prints into it counts that figure twice — 33.62
  measured plus a 31.42 "offset" published 65.04, roughly double the truth, on a
  public leaderboard. If you do not know the pre-collection number, leave it
  `0`; a total that is honestly short is worth more than one that is silently
  doubled.

  To derive it, ask AlphaESS directly for the days before the record starts.
  `getOneDateEnergyBySn` serves history from the install date and returns zeros
  before it, so walking backwards finds both the first operating day and the
  throughput that predates `daily_energy`:

  ```sh
  sudo docker compose run --rm collector python - <<'EOF'
  import datetime as dt, os
  from efficiency import fetch_day_energy
  aid, sec, sn = (os.environ["ALPHAESS_APP_ID"], os.environ["ALPHAESS_APP_SECRET"],
                  os.environ["ALPHAESS_SYS_SN"])
  # The local days before your earliest daily_energy row.
  total = 0.0
  for d in range(1, 18):
      day = dt.date(2026, 7, d)
      dis = float((fetch_day_energy(aid, sec, sn, day) or {}).get("eDischarge") or 0)
      total += dis
      print(day, dis)
  print("offset =", round(total / float(os.environ["BATTERY_CAPACITY_KWH"]), 2), "cycles")
  EOF
  ```

  One call per day at `ALPHAESS_MIN_REQUEST_INTERVAL_S`, and that budget is
  shared with the live collector — keep the range to the days that actually
  predate the record. On this installation the answer was 0.37 cycles: the pack
  first ran on 2026-07-16 and `daily_energy` starts at 2026-07-18, leaving two
  days (10.30 kWh) that no row covers and that the fill will not invent, because
  it deliberately does not reach back past the first stored day.
- **`batteryResultTotal`.** The sum of stored `daily_cost.saving` at the
  current `model_version`, plus today so far. Days that failed
  `pricing.gate()` are absent from `daily_cost` and therefore from this total;
  it under-states rather than estimates, deliberately.

`chargedToday`/`dischargedToday` skip any gap in `power_readings` longer than
`MIJNBATTERIJ_MAX_SAMPLE_GAP_S` — leave it **blank**, which derives it as 3× the
collector's `POLL_INTERVAL_SECONDS` so it follows the poll interval instead of
being pinned to whatever 3× was the day it was written — rather than
interpolating across it — interpolating six missing hours at 4 kW would invent
~24 kWh. So after a collector outage those two figures under-report the day, by
the number of seconds `gap_skipped_s` on the `mijnbatterij_submit` point names.

### 3. Publish

```sh
sudo docker compose up -d --build mijnbatterij
```

Not a bare `up -d`, which recreates the collector and cost 922 s of samples on
2026-08-10. `--build` because `up` reuses an existing image too: without it a
pulled change starts nothing new, and the container comes up looking healthy
while running the previous version.

Verify it from InfluxDB rather than from the site, which caches:

```sh
sudo docker compose logs --tail 20 mijnbatterij
```

Every cycle also writes a `mijnbatterij_submit` point — `submitted`,
`status_code`, `price_coverage`, `gap_skipped_s` and the figures actually sent —
so a rejection is visible in Grafana instead of only in a log that rotates. The
`outcome` tag says what happened: `ok`, `rejected`, `unreachable`, `error`,
`stale` (the collector stopped), `no-data` (nothing in the bucket for this
serial at all) or `day-start` (the benign seconds after midnight, before the
day's first poll — the only one that pushes no heartbeat).

A `batteryResult` of exactly 0 with `price_coverage` below 0.999 is not a
break-even day: it means `market_price` does not cover the day so far, so the
euro figure was suppressed rather than published as a fraction of itself. Check
`refresh-prices.sh`.

### Monitoring the submissions

`MIJNBATTERIJ_HEARTBEAT_URL` takes an Uptime Kuma **Push** monitor URL:

- **Heartbeat Interval**: `600`
- **Retries**: `1`, **Heartbeat Retry Interval**: `300`

The interval is twice `MIJNBATTERIJ_INTERVAL_SECONDS` on purpose. One missed
cycle must not page you, two must; `Retries: 1` puts the first miss in PENDING
rather than a notification. Worst case the alert lands ~15 minutes after
submissions stop, which is the right speed for a leaderboard.

| Cycle outcome | Push | What it actually means |
|---|---|---|
| Submitted | `up` | — |
| `stale` | `down`, first cycle | **the collector is down**, not this service |
| `no-data` | `down`, first cycle | wrong `ALPHAESS_SYS_SN`, or the token cannot read the bucket |
| `day-start` | nothing | benign, the ~30 s after midnight before the day's first poll |
| Rejected 4xx/5xx | `down` from the 2nd consecutive | carries the platform's own validation message |

The two `down`-on-the-first-cycle rows are why this monitor fires alongside the
collector's own `HEARTBEAT_URL` during a NAS outage. That duplication is the
design: a submitter that stays green while publishing nothing for six hours is
the precise failure this exists to catch, and it cannot distinguish "no data
because the collector stopped" from "no data" without asking the collector,
which is the thing that just broke. Route it to a separate notification group
if the doubling is noise; do not soften the check.

The rejection message is worth reading. The spec (["The API is documented, after
all"](#the-api-is-documented-after-all)) covers the fields, but the server is
what decides, and its 400 text is the most specific description of a
disagreement anyone gets.

**Use an IP address, not a hostname.** The container resolves through Docker's
resolver, which has neither the NAS's `search` domain nor mDNS, so a bare
`data42` or a `.local` name fails with `Failed to resolve` on every ping — and
because heartbeat errors are logged and swallowed by design, the only symptom
is a monitor that never goes green:

```
WARNING mijnbatterij: Heartbeat push failed: HTTPConnectionPool(host='data42',
port=3001): ... Failed to resolve 'data42' ([Errno -2] Name or service not known)
```

Tailscale does not help here. MagicDNS resolves on the *host*; the container is
on a Docker bridge network and never sees that resolver. Use the NAS's LAN IP
(`ip -4 addr show eth0`), or `100.x.y.z` from `tailscale ip -4` if you want the
tailnet address — both are addresses, which is the point.

**Paste the URL Kuma shows you, query string and all.** `send_heartbeat`
rebuilds the query rather than appending to it, so the trailing
`?status=up&msg=OK&ping=` is harmless. It was not always: appending makes
Express parse `status` as `["up", "down"]`, which matches neither value, so
every ping — including the `up` ones — registers DOWN and the message renders
as `[object Object]`. This repo has now fixed that bug three times
(`collector/collector.py:407`, `dispatch/heartbeat.py:22`, and here). It keeps
coming back because the broken call reads more naturally than the fix.

### Backfilling finished months

`/api/live` publishes today. History goes to two other endpoints, and the split
matters: **`/api/results/daily` carries the per-day figures, and
`/api/results/monthly` carries month TOTALS only.** The monthly endpoint has no
per-day structure at all — an earlier version of this integration sent a `days`
map reverse-engineered from a third-party example and got
`400 Invalid request provided` for it.

```sh
cd /volume1/docker/alphaess-collector
git pull
sudo docker compose build mijnbatterij collector
sudo docker compose run --rm mijnbatterij python mijnbatterij.py --monthly 2026-08 --dry-run
```

**`compose run` does not build.** It uses whatever image already exists, so a
`git pull` on its own leaves you running the code from the last build — which
surfaces as `error: unrecognized arguments: --monthly`, i.e. an image predating
the flag rather than anything wrong with the command. Build first whenever the
source has moved, and build `collector` too: Compose builds one image per
service, so despite the shared `./collector` build context they are two images
and `pricing.py` runs from the other one.

`--dry-run` prints the exact bodies and contacts nothing. To check them against
the real API without storing anything, use the API's own testing flag instead:

```sh
sudo docker compose run --rm mijnbatterij python mijnbatterij.py --monthly 2026-08 --test
```

`--test` sets `test: true` on every day payload, which the API validates and
discards. That is the honest way to confirm a schema change — there is a
published spec now (below), but it is the server that decides.

Read the two warnings before submitting for real:

- **"had no daily_energy row and were integrated from power_readings"** — the
  same repair the live path makes. Those days go out flagged `invalid`, which
  is the spec's own field for a figure that may be incomplete.
- **"have no stored daily_cost row, sent as €0.00 flagged invalid"** — the
  month's euro total is short by those days. Check *why* before publishing,
  because the two causes have different answers:

  ```sh
  sudo docker compose run --rm collector \
    python pricing.py --backfill 2026-08-29 2026-08-31 --dry-run
  ```

  A day reported `EXCLUDED (coverage …)` is gated and will never have a row —
  it stays €0.00, deliberately, for the reason in
  [docs/MIJNBATTERIJ-FLOW.md](docs/MIJNBATTERIJ-FLOW.md). A day that computes a
  saving cleanly simply has not been written yet, because the nightly job has
  not run since. Write it first, then submit:

  ```sh
  sudo docker compose run --rm collector python pricing.py --date 2026-08-31
  sudo docker compose run --rm mijnbatterij python mijnbatterij.py --monthly 2026-08
  ```

On 2026-09-01 this installation's August came to 31 days, 747.4 kWh charged,
713.6 kWh discharged, €115.25, marked `partial` because 2026-08-29 carries no
euro figure (gated at coverage 0.808 after a 35-minute outage) and four days'
energy is derived from `power_readings`.

**Only whole past days are sent.** Today is capped out of the payload: it is
still moving, it belongs to `/api/live`, and the spec requires the daily
endpoint's `date` to be before today in Europe/Amsterdam anyway. Re-running a
month is safe and is how a day gets corrected once its nightly row lands —
nothing is ever sent `finalized`, which the spec makes permanent.

### Publishing finished days nightly

`--monthly` above is also the scheduled job. The live loop publishes **today**
every 5 minutes and nothing else; until this task is scheduled, the platform's
record of *yesterday* is whatever the last `/api/live` snapshot before midnight
carried — the day truncated at the last submission, computed before
`daily-savings.sh` had written its euro figure at all.

Schedule [scripts/daily-mijnbatterij.sh](scripts/daily-mijnbatterij.sh):

- **General**: User = `root` (DSM's docker socket needs root)
- **Schedule**: Daily, first run time `03:30`
- **Task Settings → Run command**:

  ```sh
  /volume1/docker/alphaess-collector/scripts/daily-mijnbatterij.sh
  ```

`03:30`, **after** `daily-savings.sh` at 02:00 and `daily-efficiency.sh` at
03:00. This job only publishes what those two wrote: run it earlier and
yesterday goes out as €0.00 flagged `invalid`, which is a true statement about
an empty `daily_cost` and a wrong one about the day.

It posts the month **yesterday** falls in — on the 1st that is the month that
just ended, so a month gets its final pass the night after its last day. For the
first `HEAL_DAYS` (4) days of a month it posts the previous month as well,
matching `daily-savings.sh`'s 4-day self-healing window: a day skipped for late
prices can be written two nights later, and by then the month-of-yesterday rule
alone would have stopped looking at it.

Safe to re-run, and safe to run while the live loop is running — nothing is ever
sent `finalized`, so a day is corrected rather than duplicated. The script exits
0 without doing anything when `MIJNBATTERIJ_API_KEY` is blank, so it can be
scheduled on an installation that has not been registered yet.

A day the platform rejects is logged by date and skipped, not fatal: the
remaining days and the month totals still go, and the run ends with exit 1 so the
notification still fires. So the failure to act on is "the same date appears in
the log every night", not "the job stopped".

**No Kuma monitor for this one**, deliberately. `MIJNBATTERIJ_HEARTBEAT_URL`
belongs to the live loop and expects a push every 5 minutes; a nightly job
pushing to the same monitor would hold it green through a dead loop. DSM's own
task-failure notification is the alert here.

Run it once by hand first:

```sh
sudo /volume1/docker/alphaess-collector/scripts/daily-mijnbatterij.sh
```

### The API is documented, after all

A 400 from the monthly endpoint named
[https://onbalansmarkt.com/help/api-docs/](https://onbalansmarkt.com/help/api-docs/),
which is a Quarkus OpenAPI UI; the machine-readable spec is at
`/help/api-docs/public-schema` (YAML, despite the JSON-ish path). It is the only
authoritative description of this API and it contradicts the reverse-engineered
payload this integration started from in four ways:

| | Reverse-engineered | Actually |
|---|---|---|
| numeric fields | JSON numbers | **strings** — `"8.80"`, `"143"` |
| `loadBalancingActive` | boolean | **`"on"` / `"off"`** |
| `batteryPower` | sent every cycle | **does not exist**; the API ignored it |
| per-day history | `days` map on monthly | **`POST /api/results/daily`** |

It also documents fields worth sending that nobody had guessed: `gridImportToday`
and `gridExportToday` (which enable the platform's 15-minute household energy
tracking), `solarKwhGenerated`, `batterySavings`, `invalid`, `partial`, `note`,
and the `test` flag above. All of those are now populated from data this stack
already holds.

### What `mode` should say — nothing, by default

`MIJNBATTERIJ_MODE` is **blank** by default, which omits the field. The spec is
explicit about why that is right: mode "can be provided to **override** the mode
configured on the profile page". The profile already carries **Modus**
(`Handmatig/doe-het-zelf`) beside **Aansturing** (`Frank Energie`) — two
different fields, and it is Modus, not Aansturing, that says this installation
is self-dispatched. Sending nothing lets it stand.

The published enum is `imbalance`, `imbalance_aggressive`, `manual`,
`day_ahead`, `self_consumption`, `self_consumption_plus`, and **which subset an
account may use varies by provider**. The first live submission from here
asserted `self_consumption` and came back:

```
400 {"message":"Battery mode: self_consumption is not available for frank-energie"}
```

If you ever do want to send one, `manual` is the value matching
`Handmatig/doe-het-zelf`, and `day_ahead` describes what the dispatcher actually
does. A value outside the enum is refused locally with a log line naming the
valid set, rather than sent to earn a 400 every cycle:

```sh
cd /volume1/docker/alphaess-collector
sed -i 's|^MIJNBATTERIJ_MODE=.*|MIJNBATTERIJ_MODE=|' .env     # blank = omit
sudo docker compose up -d --build mijnbatterij
```

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

**Each night's backup is one host-written `influxdb-<date>.tgz`, and that shape
is load-bearing.** Synology Cloud Sync never notices files written from inside
a container through a bind mount, so a backup left where `influx backup` wrote
it stays on the NAS and never reaches the cloud — with the local files all
present and correct, which is what made this hard to spot. Nor does a
host-written marker file rescue the folder around it: Cloud Sync uploads the
individual writes it saw and nothing else. So `influx backup` writes into
`$BACKUP_HOST_DIR/.staging`, which is not expected to sync, and the host tars
that into the archive, which is. Anything added to this tree in future must be
written by the host, or it will sit there unsynced. BACKUP-DATABASE.MD,
"Making Cloud Sync notice", has the full account.

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

`influx restore` reads a directory, so unpack the chosen archive into the
staging folder the container already sees — it is scratch space, emptied at the
start of every backup run:

```sh
cd /volume1/docker/alphaess-collector
STAGING="$BACKUP_HOST_DIR/.staging"
rm -rf "$STAGING" && mkdir -p "$STAGING"
tar -xzf "$BACKUP_HOST_DIR/influxdb-<date>.tgz" -C "$STAGING"

docker compose exec influxdb influx restore "/backups/.staging" \
  --full --token "$INFLUX_TOKEN"

rm -rf "$STAGING"/*
```

Clear it afterwards as shown. Unlike the nightly job, this extraction *is* a
host write, so leaving it in place would push a second full copy of the backup
up to Drive.

**No `--org` on that command.** `--full` restores every org in the backup and
the CLI refuses to have that narrowed — `Error: --full restore cannot be
limited to a single org or bucket`. This page carried the `--org` form until
the restore drill below was written and ran it for the first time.

`--full` also replaces the key-value store, tokens included, and then keeps
going using the credentials it started with. That works here only because
`INFLUX_TOKEN` is both the admin token of the running server and the admin
token inside the backup; restore an archive into a server set up with a
*different* admin token and it dies midway with `failed to restore SQL
snapshot: 401 Unauthorized`, leaving it half-restored.

### Verifying a backup restores

A backup nobody has restored is a backup nobody knows works. Everything above
only establishes that a file was written and reached Drive, which says nothing
about whether it can be read back.

[scripts/verify-influxdb-backup.sh](scripts/verify-influxdb-backup.sh) answers
that without touching the live database. It starts a throwaway `influxdb`
container — its own volume, no published port, not on the compose network —
restores an archive into it, checks the `alphaess` bucket came back with recent
data in it, and destroys the container:

```sh
cd /volume1/docker/alphaess-collector
sudo ./scripts/verify-influxdb-backup.sh                    # newest archive
sudo ./scripts/verify-influxdb-backup.sh /path/to/one.tgz   # a specific one
```

On success it prints roughly this — the point count and timestamp are the part
worth reading, since a bucket can restore empty:

```
verify-influxdb-backup: PASS
  bucket:  alphaess restored
  points:  <n> in the last 7d
  newest:  <most recent timestamp in the restored copy>
  archive: .../influxdb-2026-08-10.tgz
```

It exits non-zero on any failure, so it can be run from Task Scheduler, but it
is deliberately not scheduled: it takes a couple of minutes and pulls a full
InfluxDB image up. Run it by hand after changing anything in the backup path,
and once in a while regardless.

`DRILL_RANGE` (default `7d`) sets how far back it looks for data. Shorten it to
watch the check fail on purpose — `DRILL_RANGE=1s ./scripts/verify-influxdb-backup.sh`
must report FAIL, and a verification you have never seen fail is not one you
can trust.

Note that the drill extracts to a `mktemp` directory rather than to
`$BACKUP_HOST_DIR/.staging` as the manual restore above does, precisely so a
rehearsal leaves nothing behind for Cloud Sync to pick up.

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
| `INFLUX_TOKEN_DISPATCH` | read `planning`, read + write `alphaess` | Reads the plan the translator consumes and writes the `dispatch_state` readback behind the dashboard's dispatch panels. The only process in the stack that reads another project's bucket, which is why it is not the collector's token. **Needed before any compose subcommand works**, including ones that have nothing to do with dispatch — see below. |
| `INFLUX_TOKEN_MIJNBATTERIJ` | read + write `alphaess` | Reads `power_readings`, `market_price`, `daily_cost` and `daily_energy` to build the mijnbatterij.nl payload; writes only its own `mijnbatterij_submit` and `mijnbatterij_rank` status series. Separate from the collector's because it is the one token in the stack that is also the credential for an outbound public submission — revoking it stops the publishing without touching collection. **The one token without a `:?` guard**, because that service is opt-in: see below. |

None of them has a fallback — except `INFLUX_TOKEN_MIJNBATTERIJ`, below.
Compose fails to start and names the missing variable. A service that quietly
reverted to the admin token would defeat the point.

**`INFLUX_TOKEN_MIJNBATTERIJ` is `:-`, not `:?`.** The guard is stack-wide, and
that service is opt-in — it idles without `MIJNBATTERIJ_API_KEY`. Guarding it
would break every compose subcommand on the NAS the moment the branch is pulled,
for a feature nobody had switched on yet, and would leave you needing a
placeholder just to run the `compose exec influxdb` that mints the real token.
Nothing is given up: the guards exist so a missing token cannot silently become
the *admin* token, and an empty value is not the admin token — it fails to
authenticate. `mijnbatterij.py` exits at startup with a message naming this
variable if the token is empty while an API key is set, which puts the error in
the one service it concerns.

That guard is stack-wide, not per-service. Compose interpolates the whole file on
every subcommand, so a missing `INFLUX_TOKEN_DISPATCH` stops `docker compose
restart grafana` — a command that touches neither dispatch nor Influx. The error
names the variable and points here, which is the guard working; it is not a
broken checkout. Until you mint the real token, an inert placeholder in `.env` is
enough to unblock the rest of the stack, because `dispatch` only ever starts on an
explicit `up -d dispatch`:

```
INFLUX_TOKEN_DISPATCH=placeholder-not-yet-minted
```

CI cannot catch this: `.github/workflows/ci.yml` copies `.env.example`, which
carries a placeholder for every key. A new `:?` guard is therefore green in CI and
blocking on the NAS until the key is added to the real `.env` — add both in the
same change.

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

sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "mijnbatterij: rw alphaess" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$ALPHAESS_ID"

# Only if you set up the Uptime Kuma keyword monitor. This one is pasted into
# Kuma's config, not into .env.
sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "uptime-kuma: r alphaess" \
  --read-bucket "$ALPHAESS_ID"
```

Copy each printed token into the matching `.env` variable, then `sudo docker
compose up -d`. The Grafana token is created the same way but should list a
`--read-bucket` for every bucket it charts — see below.

### `INFLUX_TOKEN_DISPATCH`

The dispatcher's token is minted separately because it is the only one spanning
two buckets, so it cannot be created until the `planning` bucket exists — see
["The `planning` bucket"](#the-planning-bucket) below. Run that section first.

```sh
cd /volume1/docker/alphaess-collector
set -a; . ./.env; set +a

ALPHAESS_ID=$(sudo docker compose exec -T influxdb influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name "$INFLUX_BUCKET" --hide-headers | awk '{print $1}')
PLANNING_ID=$(sudo docker compose exec -T influxdb influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name planning --hide-headers | awk '{print $1}')
echo "alphaess: $ALPHAESS_ID  planning: $PLANNING_ID"

sudo docker compose exec -T influxdb influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "dispatch: r planning, rw alphaess" \
  --read-bucket "$PLANNING_ID" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$ALPHAESS_ID"
```

Both `echo`ed ids must be non-empty. `influx bucket list --name` on a bucket that
does not exist prints nothing and exits 0, so a typo yields an empty `--read-bucket`
argument and a token scoped to less than you asked for — which surfaces much later
as the dispatcher reading no plan and holding at self-consumption.

**Read on `planning`, never write.** The plan is the other project's output and
this one only consumes it; a write scope here would let a dispatcher bug corrupt
the input it is reading. Write is on `alphaess` alone, for the `dispatch_state`
readback behind the dashboard's dispatch panels.

Put it in `.env` as `INFLUX_TOKEN_DISPATCH`, replacing any placeholder, then start
the service on its own:

```sh
sudo docker compose up -d dispatch
```

Not a bare `up -d`, which recreates the collector and cost 922 s of samples on
2026-08-10. The service starts in **dry run** regardless (`DISPATCH_LIVE` defaults
to 0); going live is a separate, deliberate step — see `DISPATCH-GOLIVE.md`.

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

- **Its logs are already collected, and it does not have to do anything.** Alloy
  reads every container on this NAS from the Docker daemon, so a new project shows up
  in Grafana → NAS → Container Logs within about 15 seconds of starting, tagged with
  its compose project name — no logging config, and not even a network to join. The
  only requirement is that it logs to stdout/stderr under the default `json-file`
  driver. See "Container logs" below.

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

Once this bucket exists, mint `INFLUX_TOKEN_DISPATCH` too — see
["`INFLUX_TOKEN_DISPATCH`"](#influx_token_dispatch) above. It is the third reader
of `planning` and the reason that section could not be run before this one.

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
writes `plan` hourly, and `report_day.py` writes `plan_score` at 06:10 for the
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
2-minute backoff cap (`MAX_BACKOFF_SECONDS`, `DEFAULT_MAX_BACKOFF_S = 120`),
so ~10 minutes is a sensible floor or you will get
false alarms on a transient blip.

The pings carry a status and a message, so the alert explains itself:

| When | Push | Notification reads |
|---|---|---|
| Successful poll | `status=up&msg=OK` | — |
| 2nd+ consecutive failure | `status=down` + the error | `ReadTimeout: HTTPSConnectionPool(host='openapi.alphaess.com'...): Read timed out. (read timeout=30)` |
| 3rd+ consecutive failure | the error + a verdict | `SSLError: SSLEOFError(8, '[SSL: UNEXPECTED_EOF...' (3 consecutive failures) [upstream]` |
| First poll after an outage | `status=up` + duration + the cause | `OK (recovered after 5 failures, 12m11s; fetch: ConnectionError: NameResolutionError... [local-dns])` |

The first failure never pushes `down` — a single failed poll is usually an
upstream blip the next poll rides out, and paging on it means being woken for
something already fixed. From the second onwards the grace period would expire
anyway, so this only changes *what the alert says*, not when it fires.

**Why the recovery message repeats what the `down` message already said.**
Because the `down` message is the one you cannot count on receiving. On
2026-08-10 the collector failed three times in twelve minutes, every time on
DNS (`Failed to resolve 'openapi.alphaess.com'`), and Kuma delivered none of
the three `down` notifications:

```
[MONITOR] ERROR: Cannot send notification to My Telegram Alert (1)
Error: getaddrinfo EAI_AGAIN api.telegram.org
```

Sending to Telegram means resolving `api.telegram.org` through the resolver
that had just failed, from a container on the same NAS — and Kuma has no retry
queue, so a notification that throws is simply gone. All three `up` messages
arrived: by then DNS was working again, which is exactly what "up" means. The
result was a phone showing three identical `OK (recovered after 2 failures,
3m00s)` and no way to tell what had happened.

That is not a DNS quirk. Any outage of the link the NAS reaches the internet
through takes out the notification channel and the monitored path together,
and the recovery notification is the one sent from the other side of it. So
the message that survives by construction is the one that has to carry the
diagnosis. It is the same argument as "Why this duplicates check 3" under
check 4 below, applied to a channel rather than a check: the duplication is
the point.

Nothing about this is worth fixing on the delivery side. Pinning Telegram's
addresses in `extra_hosts` trades a transient failure for a silent permanent
one the day they rotate, and no local configuration reaches Telegram while the
uplink is down.

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
same bucket, tagged `event` (`failure`/`recovered`/`heartbeat_failed`),
`error_class`, and `stage` — `fetch` or `write` for a poll, `heartbeat` for a
push that never arrived. InfluxDB
is local, so it keeps accepting writes precisely when the AlphaESS API is
unreachable — it records the outage while it is happening. The
**Collector Health** dashboard
([`grafana/alphaess-collector-health.json`](grafana/alphaess-collector-health.json))
reads it: failed polls, outage count and duration, failures split by error
class, and a table of the actual error messages. It is provisioned
automatically (bind-mounted into `dashboard-src` like the other dashboards).
That table is the answer to
"what did the alert mean", reachable from a phone instead of
`docker compose logs`. Writes are best-effort and never fail a poll; if
InfluxDB itself is the thing that is broken, nothing is recorded (the
heartbeat still fires, which is the point of having both).

`heartbeat_failed` is the mirror image of that last sentence: a push to Uptime
Kuma that never arrived, written here because the collector is the only party
that knows. It is the one failure the rest of this section cannot see — the
collector is healthy, the data is arriving, every panel is green, and nobody is
watching. Observed on 2026-08-29, when the Kuma host changed IP and `.env` was
updated without recreating the container, so every ping for hours went to the old
address and left nothing but log lines. The **Heartbeat unreachable** tile on the
Collector Health dashboard counts these, and
[`grafana/provisioning/alerting/alphaess-heartbeat-unreachable.yml`](grafana/provisioning/alerting/alphaess-heartbeat-unreachable.yml)
pages after ten minutes of them. When it fires, remember that compose reads env
only at container start: the fix is `up -d --force-recreate`, not `restart`, and
every other `*_HEARTBEAT_URL` points at the same host and needs the same
treatment.

**3. Grafana staleness alert (read side, secondary).** The rule in
[`grafana/provisioning/alerting/alphaess-staleness.yml`](grafana/provisioning/alerting/alphaess-staleness.yml)
fires when the newest `power_readings` sample is more than 5 minutes old, and
on no data at all. The bundled Grafana mounts `./grafana/provisioning`, so the
rule is picked up automatically — nothing to install.

The same 5-minute staleness rule is rendered on screen as the **Collector
status** stat, first in the top row of the *Collector Health*
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

## Container logs

Every container on this NAS logs to Docker's `json-file` driver and nowhere else, so an
error is only ever found by someone who already suspected that container.
[**nas-observability**](https://github.com/sandeepthukral/nas-observability) fixes
that: **Alloy** reads each container's log stream from the Docker daemon and **Loki**
stores it for 30 days, and this repo's Grafana reads it.

Nothing about how the containers log changes. `sudo docker logs <container>` still works
and is still the fallback for when the log stack itself is what is down.

### Starting it

```sh
cd /volume1/docker
git clone https://github.com/sandeepthukral/nas-observability.git
cd nas-observability
sudo docker compose up -d
```

`alphaess-net` must already exist, which it does whenever this stack has been started
once. Then restart Grafana so it picks up the new datasource and dashboard:

```sh
cd /volume1/docker/alphaess-collector
sudo docker compose up -d grafana
```

`up -d grafana`, not a bare `up -d` — the latter recreates the collector and costs a few
minutes of samples.

Open **Dashboards → NAS → Container Logs**. The "Errors and warnings" panel is the one
this exists for.

### What is collected

**Every container on the NAS, in every compose project, with no configuration in that
project.** Alloy asks the Docker daemon for the logs of everything it can see, so a
container qualifies purely by running here — not by joining `alphaess-net`, not by having
a Grafana, not by being named anywhere.

Two things leave a container out, and both are silent:

- **It writes to a logfile inside itself rather than to stdout/stderr.** `sudo docker logs
  <container>` coming back empty is the tell.
- **Its compose sets another log driver.** `json-file` and `local` are collected; `none`,
  `syslog` and `gelf` are not:
  ```sh
  sudo docker inspect -f '{{.HostConfig.LogConfig.Type}}' <container>
  ```

**The first start replays history.** On an empty positions file Alloy reads each
container's whole retained `json-file` log, not just new lines — so a container without a
rotation cap hands over months at once. Loki refuses anything past
`reject_old_samples_max_age` and Alloy logs a `400` per rejected batch. That looks alarming
and is not: the valid entries in each batch are still stored, and it drains in about a
minute. Observed on the first NAS start 2026-08-30, which replayed months of sonarr,
radarr and teslamate history and left a 20 MB store.

What is genuinely lost is anything older than the 30-day window, and anything written
while Alloy is down that rotates away before it returns. `docker logs` is the only route
to those.

### Which half lives where

Grafana provisioning cannot be owned by two repos, so the feature is split:

| Piece | Repo |
|---|---|
| `loki` and `alloy` services, their configs | [nas-observability](https://github.com/sandeepthukral/nas-observability) |
| `grafana/provisioning/datasources/loki.yml` | here |
| `grafana/nas/nas-logs.json` (the dashboard) | here |
| `grafana/provisioning/alerting/nas-log-errors.yml` | here |

The dashboard queries `container`, `project` and `service` — labels set in
nas-observability's `config.alloy`. Renaming one there breaks the queries here, and
nothing checks that across the two repos.

### Notification

`nas-log-errors.yml` provisions the rule only, like the two rules beside it: Grafana shows
**Container logging errors** firing per-container after five continuous minutes of more
than three error-level lines, and delivery is yours to wire up.

**For Uptime Kuma, poll rather than push.** A Kuma *push* monitor goes red when a ping
stops arriving, so a Grafana contact point that fires only during an alert produces a
monitor that is red whenever things are fine — backwards, and it takes a while to notice
why. Use a **HTTP(s) - Json Query** monitor instead, pointed at Loki:

- URL: `http://loki:3100/loki/api/v1/query`
- Query parameter `query`:
  `sum(count_over_time({container=~".+"} | detected_level =~ "(?i)(error|critical|fatal)" [5m]))`
- Json Query: `$.data.result[0].value[1]`, expected value `0`

Kuma must be on `alphaess-net` to resolve `loki`. This inverts the direction — Kuma polls,
nothing has to push — and red then means what it looks like it means.

The alternative is a normal Grafana contact point (email, ntfy, a webhook) attached to the
default notification policy, which is what the note at the end of "Monitoring that the
collector is actually collecting" already describes.

### Severity is matched case-insensitively, on purpose

Two things classify a line and they disagree on case. Alloy parses the Python services in
this repo directly, so their level arrives spelled as Python spells it (`ERROR`); Loki's
own heuristic handles everything else and emits lowercase (`error`). Any query you write
by hand must therefore use:

```
| detected_level =~ "(?i)(warn|warning|error|critical|fatal)"
```

`detected_level = "error"` returns half the errors on the NAS, with nothing to say so.
`tests/test_grafana_provisioning.py::test_every_severity_filter_is_case_insensitive`
pins this for the committed queries.

### Querying Loki by hand

`exec` into **loki**, not alloy — Alloy's image is distroless and has no `wget`, so the
obvious command fails with "executable file not found".

```sh
cd /volume1/docker/nas-observability
sudo docker compose exec loki wget -qO- \
  'http://localhost:3100/loki/api/v1/label/project/values'
```

That lists every compose project currently reaching Loki, which is the fastest check that
collection is host-wide rather than just this stack. A project missing from it is one
whose log driver needs checking, per "What is collected" above.

### Retention

30 days, set in `loki-config.yaml` as `retention_period: 720h`. That only takes effect
because `compactor.retention_enabled: true` is set beside it — retention is enforced by
the compactor, and the limit alone is silently ignored. Confirm both landed:

```sh
sudo docker compose exec loki wget -qO- http://localhost:3100/config | grep -A3 retention
sudo docker system df -v | grep loki
```
