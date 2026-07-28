# NAS migration runbook

Live working document for deploying the current round of changes to the Synology
NAS (`/volume1/docker/alphaess-collector`).

**How this document is used:** work through the steps in order. After each one,
we revisit this file together — correcting steps that turned out wrong, and
adding steps the outcome revealed. It is not a plan written once up front; it is
expected to change as we go. The revision log at the bottom records what changed
and why.

- **Status:** not started
- **Last updated:** 2026-07-28
- **Covers:** price-coverage gate (`MODEL_VERSION` 1 → 2), collector fetch/write
  failure split, test scaffolding

---

## What is changing, and what it means for your data

| Change | Effect on the NAS |
| ------ | ----------------- |
| Price-coverage gate + `MODEL_VERSION = "2"` | `daily_cost` history must be recomputed to appear on the dashboard. Existing version-1 rows are **kept**, not deleted. |
| Dashboard **Model version** variable → `2` | Grafana must restart to pick up the new dashboard JSON. |
| Collector fetch/write failure split | Behaviour change on failure only. Nothing to migrate. |
| Tests / CI / `pyproject.toml` | Developer-only. Never enters an image. |

Two facts worth holding on to:

- **Raw data is untouched.** `power_readings` and `market_price` are not
  modified, deleted, or reinterpreted by any of this.
- **The arithmetic did not change.** A day that was fully priced computes
  bit-identically at version 1 and version 2. Recomputing exists to *prove* each
  day's prices were complete, not to correct a formula.

A day that ends up present at version 1 but missing at version 2 is precisely a
day whose prices could not be verified as complete — one that was silently
understated before.

**No `.env` changes are required.** The new `PRICING_MIN_PRICE_COVERAGE` knob
defaults to `0.999`.

---

## Steps

### 0. Get the changes onto the default branch

`simplify-nas-deploy` is already merged (PR #15) and is **not** part of this
round. This round is branch `review-quality-gate-and-tests`, four commits on
top of `main`:

| Commit | |
| ------ | - |
| `test:` | pytest scaffolding, ruff config, CI |
| `fix(pricing):` | price-coverage gate, `MODEL_VERSION` 1 → 2, `--audit` |
| `fix(collector):` | fetch/write failure split |
| `docs:` | review findings, this runbook |

Pushed and awaiting review. Merge it to `main` before pulling on the NAS —
step 2 pulls `main`, so nothing below takes effect until it lands.

- [ ] Branch pushed
- [ ] Merged to `main`

### 1. Back up the InfluxDB volume

Everything below is additive and reversible, but the recompute writes to the
same measurement the dashboard reads, so take the backup anyway.

```sh
cd /volume1/docker/alphaess-collector
set -a; . ./.env; set +a          # so $INFLUX_TOKEN is available below

docker compose exec influxdb influx backup /tmp/influx-backup -t "$INFLUX_TOKEN"
docker compose cp influxdb:/tmp/influx-backup ./influx-backup-$(date +%F)
ls -la ./influx-backup-$(date +%F)
```

The `-t` is explicit because the container's CLI config is created during first
setup and is easy to have lost across recreates; passing the token avoids
depending on it. If `influx backup` fails anyway, snapshot the volume instead —
this needs no auth at all and is just as good for rollback:

```sh
docker compose stop influxdb
docker run --rm \
  -v alphaess-collector_alphaess-influxdb-data:/data:ro \
  -v "$PWD":/backup alpine \
  tar czf /backup/influx-volume-$(date +%F).tgz -C /data .
docker compose start influxdb
```

The volume name above is correct as long as the repo sits in a directory called
`alphaess-collector` (Compose derives the project name from it, and the NAS path
matches). Confirm with `docker volume ls | grep influxdb` if unsure.

- [ ] Backup taken and copied off the container
- [ ] Backup file/directory exists and is non-empty

### 2. Pull the code

```sh
cd /volume1/docker/alphaess-collector
git pull
git log --oneline -1
```

- [ ] Working tree updated, no local modifications lost

### 3. Rebuild the images and restart

`collector` and `awtrix-pusher` bake their `.py` files in at build time, so a
plain restart would keep running the old code. Grafana needs a restart because
its entrypoint copies the dashboard JSON into place at container start.

```sh
docker compose up -d --build collector awtrix-pusher
docker compose restart grafana
```

- [ ] Both images rebuilt (build output shows the `COPY` layers re-running)
- [ ] Grafana restarted

### 4. Confirm the collector is healthy again

```sh
docker compose logs --tail=30 collector
```

Expect the startup line (`Polling every 30s for sysSn=…`) and no repeating
failures. On failure the log lines now name the stage — `Poll failed at fetch`
or `Poll failed at write`.

- [ ] Collector polling normally
- [ ] Link MTU line looks right (no warning about exceeding `EXPECTED_MAX_MTU`)

### 5. Find the first day of collected data

This is the start of the backfill range. If you already know when you deployed,
use that and skip the query.

```sh
docker compose run --rm collector python -c "
import os
from influxdb_client import InfluxDBClient
c = InfluxDBClient(url=os.environ['INFLUX_URL'], token=os.environ['INFLUX_TOKEN'],
                   org=os.environ['INFLUX_ORG'])
b = os.environ['INFLUX_BUCKET']
for fn in ('first', 'last'):
    q = f'''from(bucket: \"{b}\")
  |> range(start: 2020-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == \"power_readings\" and r._field == \"soc_percent\")
  |> {fn}()'''
    for t in c.query_api().query(q):
        for r in t.records:
            print(fn, r.get_time())
"
```

- [ ] Range noted: `START = ________`  `END = ________` (END = yesterday)

### 6. Ensure prices are complete for the whole range

```sh
docker compose run --rm collector python prices.py --backfill START END
```

Watch for `No prices returned for …` lines — those days cannot be computed and
will be excluded in the next step. That is the correct outcome, not a failure.

- [ ] Completed
- [ ] Any days reported as having no prices noted here: ________

### 7. Recompute `daily_cost` at model_version 2

No `--force` needed: no version-2 row exists yet, so every day is reprocessed.

```sh
docker compose run --rm collector python pricing.py --backfill START END
```

Each accepted day logs `wrote daily_cost`. Days that cannot be verified log
`EXCLUDED (price coverage … )` and are skipped — expected, and the whole point.

- [ ] Completed
- [ ] Count of days written: ________
- [ ] Count of days excluded: ________

### 8. Check the dashboard

Open the battery-savings dashboard.

- [ ] The **Model version** picker offers `1` and `2`, and is set to `2`
- [ ] "Days analysed to date" is close to the days-written count from step 7
- [ ] Totals look plausible against what you remember

If the picker still shows only `1`: `dashboards.yml` sets `allowUiUpdates: true`,
so a copy previously saved from the Grafana UI can win over the provisioned
file. Set the variable to `2` in the UI and save the dashboard.

### 9. Compare the two versions

Flip **Model version** to `1`, note the all-time total, flip back to `2`.

- [ ] Difference understood: any gap is days that version 2 refused to verify
- [ ] Version-1 day count minus version-2 day count = ________

### 10. Run the audit

Read-only. Confirms every stored version-2 row still passes today's gate.

```sh
docker compose run --rm collector python pricing.py --audit
```

Expect `Audited N stored day(s) at model_version=2: N OK, 0 stale`, plus a count
of superseded version-1 rows, which are fine to leave in place.

- [ ] `0 stale`
- [ ] If not zero: the printed `influx delete` commands are the fix — bring the
      output back here before running them

### 11. Confirm the nightly job still works

`scripts/daily-savings.sh` runs from DSM Task Scheduler at ~02:00. Run it once
by hand rather than waiting.

```sh
sh /volume1/docker/alphaess-collector/scripts/daily-savings.sh
```

- [ ] Runs clean, processes its 4-day window
- [ ] Re-running `pricing.py --audit` still reports `0 stale`

---

## Rollback

Nothing here is destructive, so rollback is mostly "put the old code back":

1. `git checkout <previous-commit>` in the repo directory.
2. `docker compose up -d --build collector awtrix-pusher && docker compose restart grafana`.
3. The dashboard returns to reading version 1, and every version-1 row is still
   there — the recompute only *added* version-2 rows alongside them.

Restoring the InfluxDB backup from step 1 is only needed if something outside
this runbook went wrong.

---

## Not in this round

Queued, deliberately not part of this migration:

- **Grafana admin credentials + port binding** (review finding #6) — next up.
  Will need its own steps here, and unlike everything above it changes how you
  reach Grafana, so it is kept separate.
- Findings #3, #4, #5, #7, #8, #9 from [CODE-REVIEW.md](CODE-REVIEW.md).

---

## Revision log

| Date | Change |
| ---- | ------ |
| 2026-07-28 | Created. Covers the price-coverage gate, `MODEL_VERSION` 1 → 2, and the collector failure-domain split. |
| 2026-07-28 | Step 0 rewritten. It wrongly described the work as an uncommitted tree on `simplify-nas-deploy`; that branch was merged as PR #15 and is not part of this round. Now names the four commits on `review-quality-gate-and-tests`. |
