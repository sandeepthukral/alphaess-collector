# Migration: scoped tokens and the `planning` bucket

Status: **complete on this stack (steps 0–6, 8); step 7 is on the planning
project's side**
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
| planning project | — | read `alphaess`, read+write `planning` |
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

- [x] On `main`, up to date, and this file present.

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

- [x] Load `.env` and find the running InfluxDB container:

```sh
set -a; . ./.env; set +a
INFLUXC=$(sudo docker ps --format '{{.Names}}' | grep influxdb)
echo "container=$INFLUXC bucket=$INFLUX_BUCKET org=$INFLUX_ORG token=${INFLUX_TOKEN:+set}"
```

All four *values on that one line* must be non-empty — it is a single `echo`, so
one line of output is correct. A failure looks like `container=` with nothing
after it. If `token=` is blank, see the note below.

> **If `.env` contains a `HEARTBEAT_URL` with a query string**, sourcing it in a
> shell breaks: the `&` separators make the shell background the line and treat
> the rest as separate assignments (you will see `[1]+ Done  HEARTBEAT_URL=...`
> and a stray `ping=` in `env`). Quote the value in `.env` —
> `HEARTBEAT_URL='http://host:3001/api/push/xxx?status=up&msg=OK&ping='` — which
> compose strips back off, so the container sees the same URL. Only the shell
> needs the quotes; sourcing is otherwise unaffected and later lines still load.

- [x] Note the admin token still works:

```sh
sudo docker exec -i "$INFLUXC" influx bucket list -t "$INFLUX_TOKEN" -o "$INFLUX_ORG"
```

Expect the `alphaess` bucket listed.

## Step 1 — Create the `planning` bucket

- [x] 400-day retention, per the planning project's requirement.

```sh
sudo docker exec -i "$INFLUXC" influx bucket create \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -n planning -r 400d
```

- [x] Capture both bucket IDs — `influx auth create` takes IDs, not names:

```sh
ALPHAESS_ID=$(sudo docker exec -i "$INFLUXC" influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name "$INFLUX_BUCKET" --hide-headers | awk '{print $1}')
PLANNING_ID=$(sudo docker exec -i "$INFLUXC" influx bucket list \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" --name planning --hide-headers | awk '{print $1}')
echo "alphaess=$ALPHAESS_ID planning=$PLANNING_ID"
```

Both must be non-empty 16-character hex IDs. If either is blank, stop — every
later step depends on them.

_Result:_ done 2026-07-29. `planning` created, retention reported as `9600h0m0s`
(the CLI normalises `400d` to hours — the same thing), shard group duration 168h.
Bucket IDs: `alphaess=a83cd3d221d6111b`, `planning=1430ea6bb66e9cb1`,
org `8058d984ecb8f7e2`.

## Step 2 — Mint the tokens

- [x] Four `influx auth create` calls. Each prints its token **once**.

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

_Result:_ done 2026-07-29. All four minted, and the `Permissions` column of each
confirmed the scoping at creation time. Auth IDs (for `influx auth delete`, if
one ever needs replacing — the token values are not recorded here):

| Auth ID | Description | Permissions |
|---|---|---|
| `1117dbe7d0d50000` | collector: rw alphaess | read + write `a83c…` |
| `1117dbf766550000` | awtrix-pusher: r alphaess | read `a83c…` |
| `1117dbffcc950000` | grafana: r alphaess, r planning | read `a83c…`, read `1430…` |
| ~~`1117dc0865550000`~~ | ~~planning: r alphaess, w planning~~ | deleted 2026-07-29, replaced below |
| `1117e38ce1150000` | planning: r alphaess, rw planning | read `a83c…`, read + write `1430…` |

> The planning token was re-minted the same day with **read on its own bucket**
> added, before it was ever deployed — the write-only version could not support an
> idempotent "skip runs already computed" or any self-audit. The trade is real but
> small: that project can now act on its own history, so it no longer has a
> write-only guarantee. It still cannot write `alphaess`.

> `influx auth create` prints the token **and** its permissions. Reading that
> column back is the cheapest possible check that `--read-bucket` twice actually
> produced two read grants, before any of step 3 runs.

## Step 3 — Verify each token before cutting over

The point of the whole runbook. Test with the old tokens still live, so a mistake
costs nothing.

- [x] Collector token can read **and** write `alphaess`:

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

- [x] Pusher token can read, and **cannot** write:

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

- [x] Grafana token can read both buckets:

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

_Result:_ done 2026-07-29, all three tokens verified while the admin token was
still in service. The negative check produced exactly the intended error:

```
Error: failed to write data: 403 Forbidden: insufficient permissions for write
```

The remaining five commands returned without error. One `migration_probe` point
was written by the collector token — removed in step 8.

## Step 4 — Put them in `.env`

- [x] Add three variables. `INFLUX_TOKEN` stays as it is.

```
INFLUX_TOKEN_COLLECTOR=...
INFLUX_TOKEN_PUSHER=...
INFLUX_TOKEN_GRAFANA=...
```

- [x] Confirm compose resolves before touching the running stack:

```sh
sudo docker compose config -q && echo "compose OK"
```

A missing variable fails here, by name, with a pointer to `DEPLOY.md` — which is
the whole reason there are no fallbacks.

_Result:_ done 2026-07-29. `compose OK` — the first successful `docker compose`
command since pulling this branch, and the point at which the `:?` guards stop
blocking. The running stack was still untouched at this point.

## Step 5 — Cut over

- [x] The only step with downtime. Expect well under a minute.

```sh
sudo docker compose up -d
sudo docker compose ps
```

`up -d` recreates the three services whose environment changed. InfluxDB itself is
untouched.

_Result:_ done 2026-07-29 20:07. Grafana, collector and awtrix-pusher recreated
and started in ~6 s; influxdb reported `Up 23 hours (healthy)` throughout, so it
was never restarted. Actual downtime was seconds, against the 20-minute
`MAX_GAP_S` budget.

## Step 6 — Verify the running stack

- [x] Collector polling, no auth errors:

```sh
sudo docker compose logs --tail 30 collector
```

Expect the MTU line and `Polling every 30s ...`, and **no** 401/unauthorized.

- [x] Data still arriving — the read end, not the writer's own claim:

```sh
sudo docker compose exec -T influxdb influx query \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -5m)
     |> filter(fn: (r) => r._measurement == \"power_readings\")
     |> last()"
```

- [x] Pusher healthy:

```sh
sudo docker compose logs --tail 20 awtrix-pusher
```

- [x] Grafana dashboards render, including **Battery Savings** (it reads
      `daily_cost`, so it exercises more than the live feed).

> A healthy pusher logs **nothing** at INFO after its banner: a successful push
> is `log.debug` (`pusher.py`), while an auth failure raises through
> `log.exception("Push cycle failed ...")` and stale or absent data warns. Silence
> is the pass condition, not missing output.

_Result:_ done 2026-07-29. Collector logged the MTU line and
`Polling every 30s ... bucket=alphaess` with no 401. An admin-token query
confirmed all five `power_readings` fields written at 20:09:23 — after the 20:07
restart — so the scoped collector token authenticated against the real write path.
Pusher quiet after its banner (see above). Grafana on `:3002` renders, **Battery
Savings** included — €23.58 over 11 days at `model_version` 2, with the daily
breakdown populated back to 2026-07-20, which exercises the Grafana token against
`daily_cost` and not just the live feed. Uptime Kuma still receiving heartbeats,
independent confirmation of a complete poll→write cycle.

## Step 7 — Hand over to the planning project

- [ ] Put the fourth token in the planning project's own `.env`. It never appears
      in this repository.
- [ ] That project uses `INFLUX_URL=http://influxdb:8086` and joins
      `alphaess-net` as external — see `DEPLOY.md`, "Sharing the stack".

> Its token reads `alphaess` and reads+writes `planning` — auth
> `1117e38ce1150000`. It **cannot** write `alphaess`, so a bug there cannot touch
> this project's history. Name it plain `INFLUX_TOKEN` on that side: it is the only
> token that project holds, and the `INFLUX_TOKEN_*` suffixes here exist only
> because one `.env` feeds three services.

_Result:_

## Step 8 — Clean up

- [x] Remove the probe points written in step 3. Count first, delete, then confirm
      the real measurements are untouched:

```sh
sudo docker compose exec -T influxdb influx query \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: <today>T00:00:00Z)
     |> filter(fn: (r) => r._measurement == \"migration_probe\") |> count()"

sudo docker compose exec -T influxdb influx delete \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -b "$INFLUX_BUCKET" \
  --start <today>T00:00:00Z --stop $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --predicate '_measurement="migration_probe"'

sudo docker compose exec -T influxdb influx query \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" \
  "from(bucket: \"$INFLUX_BUCKET\") |> range(start: -10m)
     |> filter(fn: (r) => r._measurement == \"power_readings\") |> count()"
```

> **`--start` is deliberately today, not the epoch.** This is the only destructive
> command in the runbook, and `influx delete` with a range but no *effective*
> predicate deletes everything in that range — irreversibly, with no tombstone to
> revert. Scoping the range to the day the probe was written means a lost or
> mangled `--predicate` costs one day, not the entire history. The final query is
> the check that actually matters: `power_readings` still counting.
>
> Skipping this step is also defensible — it is one point in a measurement nothing
> queries, and the only cost is `migration_probe` appearing in measurement lists.

- [x] Confirm the admin token is no longer handed to any service:

```sh
sudo docker compose config | grep -cF -- "$INFLUX_TOKEN"
```

> `-F --` matters: tokens are base64 and contain `.`, `+` and `/`, which a basic
> regex would interpret. Keep the pipe, too — `docker compose config` alone prints
> every secret in the file to the terminal.

Expect **1** — `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` on the influxdb service, which
is what initialises a fresh install.

_Result:_ done 2026-07-29. Probe points deleted with the range narrowed to the day
they were written; `power_readings` confirmed still arriving afterwards.
`grep -cF` returned **1** — `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` on the influxdb
service, and nothing else. The admin token is out of every application's hands.

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

- 2026-07-29 — planning token re-minted as `r alphaess, rw planning` and the
  write-only one deleted, before deployment. Read on its own bucket is what an
  idempotent "skip runs already computed" needs; cheaper to change before the other
  project is configured than after.

- 2026-07-29 — step 8's delete narrowed from `--start 1970-01-01` to the day the
  probe was written, and given count-before / verify-after queries. The epoch
  start bought nothing and made a lost `--predicate` cost the whole history.
  `grep` given `-F --` for base64 tokens.

- 2026-07-29 — steps 0–4 run on the NAS; results recorded inline. Clarified
  "all four must be non-empty" in step 0, which reads as *four lines of output*
  rather than four values on one line.

- 2026-07-29 — steps 0–3 moved off `docker compose` onto plain `docker exec`,
  found on the first run: compose interpolates the whole file on *every*
  subcommand, so the new `:?` guards blocked `exec` too, and the runbook could
  not reach the step that adds the variables. Also documented the `&` in a
  Kuma `HEARTBEAT_URL` breaking `. ./.env`.

- 2026-07-28 — written, from the planning project's stated requirements: bucket
  `planning`, 400-day retention, measurement `plan`, tag `plan_run`, ~770
  points/day, one token with read `alphaess` + write `planning`, and a single
  Grafana datasource reading both buckets so Flux can join across them.
