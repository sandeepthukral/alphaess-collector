# Migration: scoped tokens and the `planning` bucket

Status: **not started**
Last updated: 2026-07-29

> Every `docker` command here runs on the NAS and needs `sudo`.

Covers code-review findings **#5** (one all-powerful token shared by every
component) and **#12** (a separate bucket for the second project).

## What is changing

| | Before | After |
|---|---|---|
| collector | admin token | read+write `alphaess` |
| awtrix-pusher | admin token | read `alphaess` |
| Grafana | admin token | read `alphaess` + read `planning` |
| planning project | — | read `alphaess`, write `planning` |
| `INFLUX_TOKEN` | held by four services | InfluxDB init and your CLI only |

Two anchor facts:

1. **No data is touched.** This changes credentials and adds an empty bucket.
   Nothing is read, rewritten or deleted.
2. **The tokens are all created before anything is cut over.** Step 3 proves each
   one works while the old ones are still in service, so the restart in step 5 is
   the only moment anything could break.

> **Keep step 5 under ~15 minutes.** The collector writes with `SYNCHRONOUS`
> options — no client-side queue, so samples attempted while it is down are lost.
> `MAX_GAP_S` is 1200 s, so a gap over 20 minutes excludes that whole day from
> `daily_cost`. This is why the tokens are verified *before* the restart rather
> than after: a wrong token discovered at step 5 turns a one-minute restart into
> an outage.

---

## Step 0 — Preconditions

- [ ] On `main`, up to date, and this file present.

```sh
cd /volume1/docker/alphaess-collector
git pull
git log --oneline -1
```

> **Steps 0–3 deliberately avoid `docker compose`.** Once this branch is pulled,
> compose refuses *every* subcommand — `exec` and `ps` included, not just `up` —
> until the three new variables exist, because it interpolates the whole file
> before doing anything. The error is the `:?` guard working:
> `required variable INFLUX_TOKEN_COLLECTOR is missing a value`.
> Plain `docker exec` talks to the running container directly and needs no
> interpolation, so the InfluxDB work below happens on today's stack, untouched.

- [ ] Load `.env` and find the running InfluxDB container:

```sh
set -a; . ./.env; set +a
INFLUXC=$(sudo docker ps --format '{{.Names}}' | grep influxdb)
echo "container=$INFLUXC bucket=$INFLUX_BUCKET org=$INFLUX_ORG token=${INFLUX_TOKEN:+set}"
```

All four must be non-empty. If `token=` is blank, see the note below.

> **If `.env` contains a `HEARTBEAT_URL` with a query string**, sourcing it in a
> shell breaks: the `&` separators make the shell background the line and treat
> the rest as separate assignments (you will see `[1]+ Done  HEARTBEAT_URL=...`
> and a stray `ping=` in `env`). Quote the value in `.env` —
> `HEARTBEAT_URL='http://host:3001/api/push/xxx?status=up&msg=OK&ping='` — which
> compose strips back off, so the container sees the same URL. Only the shell
> needs the quotes; sourcing is otherwise unaffected and later lines still load.

- [ ] Note the admin token still works:

```sh
sudo docker exec -i "$INFLUXC" influx bucket list -t "$INFLUX_TOKEN" -o "$INFLUX_ORG"
```

Expect the `alphaess` bucket listed.

## Step 1 — Create the `planning` bucket

- [ ] 400-day retention, per the planning project's requirement.

```sh
sudo docker exec -i "$INFLUXC" influx bucket create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -n planning -r 400d
```

- [ ] Capture both bucket IDs — `influx auth create` takes IDs, not names:

```sh
ALPHAESS_ID=$(sudo docker exec -i "$INFLUXC" influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name "$INFLUX_BUCKET" --hide-headers | awk '{print $1}')
PLANNING_ID=$(sudo docker exec -i "$INFLUXC" influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name planning --hide-headers | awk '{print $1}')
echo "alphaess=$ALPHAESS_ID planning=$PLANNING_ID"
```

Both must be non-empty 16-character hex IDs. If either is blank, stop — every
later step depends on them.

_Result:_

## Step 2 — Mint the tokens

- [ ] Four `influx auth create` calls. Each prints its token **once**.

```sh
sudo docker exec -i "$INFLUXC" influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "collector: rw alphaess" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$ALPHAESS_ID"

sudo docker exec -i "$INFLUXC" influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "awtrix-pusher: r alphaess" \
  --read-bucket "$ALPHAESS_ID"

sudo docker exec -i "$INFLUXC" influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "grafana: r alphaess, r planning" \
  --read-bucket "$ALPHAESS_ID" --read-bucket "$PLANNING_ID"

sudo docker exec -i "$INFLUXC" influx auth create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -d "planning: r alphaess, w planning" \
  --read-bucket "$ALPHAESS_ID" --write-bucket "$PLANNING_ID"
```

Copy all four somewhere before moving on. They cannot be displayed again — a lost
token has to be deleted and replaced.

_Result:_

## Step 3 — Verify each token before cutting over

The point of the whole runbook. Test with the old tokens still live, so a mistake
costs nothing.

- [ ] Collector token can read **and** write `alphaess`:

```sh
COLLECTOR_TOKEN=paste_here

sudo docker exec -i "$INFLUXC" influx query \
  -t "$COLLECTOR_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -10m) |> limit(n: 1)"

sudo docker exec -i "$INFLUXC" influx write \
  -t "$COLLECTOR_TOKEN" -o "$INFLUX_ORG" -b "$INFLUX_BUCKET" \
  "migration_probe value=1"
```

Both must succeed. (The probe point is harmless — a one-off in its own
measurement, never queried by anything.)

- [ ] Pusher token can read, and **cannot** write:

```sh
PUSHER_TOKEN=paste_here

sudo docker exec -i "$INFLUXC" influx query \
  -t "$PUSHER_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -10m) |> limit(n: 1)"

sudo docker exec -i "$INFLUXC" influx write \
  -t "$PUSHER_TOKEN" -o "$INFLUX_ORG" -b "$INFLUX_BUCKET" "migration_probe value=1"
```

The read must succeed and **the write must fail** with an authorization error. A
write that succeeds means the token is not scoped as intended — stop and re-mint.

- [ ] Grafana token can read both buckets:

```sh
GRAFANA_TOKEN=paste_here

sudo docker exec -i "$INFLUXC" influx query \
  -t "$GRAFANA_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -10m) |> limit(n: 1)"

sudo docker exec -i "$INFLUXC" influx query \
  -t "$GRAFANA_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"planning\") |> range(start: -10m)"
```

The first returns rows. The second returns **no rows but no error** — `planning`
is empty. An authorization error there means the second `--read-bucket` did not
take.

_Result:_

## Step 4 — Put them in `.env`

- [ ] Add three variables. `INFLUX_TOKEN` stays as it is.

```
INFLUX_TOKEN_COLLECTOR=...
INFLUX_TOKEN_PUSHER=...
INFLUX_TOKEN_GRAFANA=...
```

- [ ] Confirm compose resolves before touching the running stack:

```sh
sudo docker compose config -q && echo "compose OK"
```

A missing variable fails here, by name, with a pointer to `DEPLOY.md` — which is
the whole reason there are no fallbacks.

_Result:_

## Step 5 — Cut over

- [ ] The only step with downtime. Expect well under a minute.

```sh
sudo docker compose up -d
sudo docker compose ps
```

`up -d` recreates the three services whose environment changed. InfluxDB itself is
untouched.

_Result:_

## Step 6 — Verify the running stack

- [ ] Collector polling, no auth errors:

```sh
sudo docker compose logs --tail 30 collector
```

Expect the MTU line and `Polling every 30s ...`, and **no** 401/unauthorized.

- [ ] Data still arriving — the read end, not the writer's own claim:

```sh
sudo docker compose exec -T influxdb influx query \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -5m)
     |> filter(fn: (r) => r._measurement == \"power_readings\")
     |> last()"
```

- [ ] Pusher healthy:

```sh
sudo docker compose logs --tail 20 awtrix-pusher
```

- [ ] Grafana dashboards render, including **Battery Savings** (it reads
      `daily_cost`, so it exercises more than the live feed).

_Result:_

## Step 7 — Hand over to the planning project

- [ ] Put the fourth token in the planning project's own `.env`. It never appears
      in this repository.
- [ ] That project uses `INFLUX_URL=http://influxdb:8086` and joins
      `alphaess-net` as external — see `DEPLOY.md`, "Sharing the stack".

> Its token can write `planning` but **not read it**, as specified. If that
> project needs to query back its own rows — an idempotent "skip runs already
> computed", or an audit — it needs read as well; the replacement command is in
> `DEPLOY.md`.

_Result:_

## Step 8 — Clean up

- [ ] Remove the probe points written in step 3:

```sh
sudo docker compose exec -T influxdb influx delete \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -b "$INFLUX_BUCKET" \
  --start 1970-01-01T00:00:00Z --stop $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --predicate '_measurement="migration_probe"'
```

- [ ] Confirm the admin token is no longer handed to any service:

```sh
sudo docker compose config | grep -c "$INFLUX_TOKEN"
```

Expect **1** — `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` on the influxdb service, which
is what initialises a fresh install.

_Result:_

---

## Rollback

Nothing is deleted or rewritten, so rollback is credentials only.

**Fastest path — no code change.** Point the three new variables at the admin
token in `.env` and restart:

```
INFLUX_TOKEN_COLLECTOR=<the admin token>
INFLUX_TOKEN_PUSHER=<the admin token>
INFLUX_TOKEN_GRAFANA=<the admin token>
```

```sh
sudo docker compose up -d
```

That restores the previous behaviour exactly, because the previous behaviour *was*
every service holding the admin token. Use it if something fails at step 5 or 6
and you want the stack healthy while you investigate.

**Full revert.** To undo the compose change as well, revert the merge commit for
this round and `up -d`. The scoped tokens can be left in place — an unused token
costs nothing — or deleted:

```sh
sudo docker compose exec -T influxdb influx auth list -t "$INFLUX_TOKEN" -o "$INFLUX_ORG"
sudo docker compose exec -T influxdb influx auth delete -t "$INFLUX_TOKEN" -i <auth-id>
```

The `planning` bucket can stay whether or not the rest is reverted; it is empty
and independent. To remove it:

```sh
sudo docker compose exec -T influxdb influx bucket delete \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -n planning
```

---

## Not in this round

- **#3** templating the hardcoded bucket in the dashboards and the alert rule
  (48 references). Independent of this work.
- **#6** the Grafana admin password fallback, and binding InfluxDB to loopback.
  The loopback change should land only once the planning project is confirmed to
  be reaching InfluxDB over `alphaess-net` rather than the host port.
- **#11** dropping `isDefault` on the datasource. Largely moot now: with a single
  shared datasource reading both buckets, there is no second datasource to
  collide with.

## Revision log

- 2026-07-29 — steps 0–3 moved off `docker compose` onto plain `docker exec`,
  found on the first run: compose interpolates the whole file on *every*
  subcommand, so the new `:?` guards blocked `exec` too, and the runbook could
  not reach the step that adds the variables. Also documented the `&` in a
  Kuma `HEARTBEAT_URL` breaking `. ./.env`.

- 2026-07-28 — written, from the planning project's stated requirements: bucket
  `planning`, 400-day retention, measurement `plan`, tag `plan_run`, ~770
  points/day, one token with read `alphaess` + write `planning`, and a single
  Grafana datasource reading both buckets so Flux can join across them.
