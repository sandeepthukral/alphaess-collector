# NAS migration runbook

Live working document for deploying the current round of changes to the Synology
NAS (`/volume1/docker/alphaess-collector`).

**How this document is used:** work through the steps in order. After each one,
we revisit this file together — correcting steps that turned out wrong, and
adding steps the outcome revealed. It is not a plan written once up front; it is
expected to change as we go. The revision log at the bottom records what changed
and why.

- **Status:** in progress — steps 0–7 done, next is step 8 (check the dashboard)
- **Last updated:** 2026-07-28
- **On the NAS, every `docker` / `docker compose` command needs `sudo`.** All
  commands below are written that way. Shell variables (`$INFLUX_TOKEN`, `$PWD`)
  are expanded by your shell before `sudo` runs, so sourcing `.env` first still
  works.
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

Merged to `main` via PR #16, so step 2's plain `git pull` picks it up — the NAS
stays on `main`, no branch to check out.

- [x] Branch pushed
- [x] Merged to `main` (PR #16)

### 1. Back up the InfluxDB volume

Everything below is additive and reversible, but the recompute writes to the
same measurement the dashboard reads, so take the backup anyway.

```sh
cd /volume1/docker/alphaess-collector
set -a; . ./.env; set +a          # so $INFLUX_TOKEN is available below

sudo docker compose exec influxdb influx backup /tmp/influx-backup -t "$INFLUX_TOKEN";
sudo docker compose cp influxdb:/tmp/influx-backup ./influx-backup-$(date +%F);
ls -la ./influx-backup-$(date +%F)
```

This is an **online** backup — InfluxDB keeps serving, the collector keeps
writing, nothing is lost. Prefer it. The `-t` is explicit because the
container's CLI config is created during first setup and is easy to have lost
across recreates; passing the token avoids depending on it.

Only if `influx backup` fails, snapshot the volume instead. This needs no auth
at all, but it **stops the database**, and that is not free:

```sh
sudo docker compose stop influxdb
sudo docker run --rm \
  -v alphaess-collector_alphaess-influxdb-data:/data:ro \
  -v "$PWD":/backup alpine \
  tar czf /backup/influx-volume-$(date +%F).tgz -C /data .
sudo docker compose start influxdb
```

> **Keep the stop under ~15 minutes.** The collector writes with `SYNCHRONOUS`
> options — there is no client-side queue, so every poll attempted while
> InfluxDB is down raises and that sample is gone for good. The resulting hole
> in `power_readings` is then judged by the very gate you are deploying:
> `MAX_GAP_S` is 1200 s, so a gap over **20 minutes excludes that whole day**
> from `daily_cost`. Backoff makes it worse — once the database is back the
> collector may still be sleeping up to 300 s before it retries, so budget the
> stop at 20 min *minus* 5 min of backoff. (`MIN_COVERAGE` 0.98 ≈ 29 min is the
> looser of the two limits; max-gap is the one that bites.) If the tar looks
> like it will run long, `sudo docker compose stop collector` first and start it
> afterwards — the gap is no smaller, but you are not also fighting backoff on
> the way out.

The volume name above is correct as long as the repo sits in a directory called
`alphaess-collector` (Compose derives the project name from it, and the NAS path
matches). Confirm with `sudo docker volume ls | grep influxdb` if unsure.

- [x] Backup taken and copied off the container — 2026-07-28, a few hours
      before the code landed. The collector has kept polling since, so the
      backup is missing those hours of `power_readings`. Good enough: rollback
      here is a code operation, nothing in steps 3–11 deletes or rewrites data,
      and step 7 only adds rows. Re-take it only if you want the belt and
      braces — `influx backup` is online and costs about a minute.
- [x] Backup file/directory exists and is non-empty

### 2. Pull the code

> **If you pulled before 2026-07-28 ~20:30 CEST, pull again.** PR #16 merged at
> that point; anything earlier is `b4d500e` or older and contains none of this
> round. This is the one step that is not idempotent-by-luck — a stale pull
> makes steps 3–7 appear to succeed while running the old code, and step 7
> would then quietly write version-1 rows.

```sh
cd /volume1/docker/alphaess-collector
git pull

# Did the code actually arrive? Both must print.
grep -n 'MODEL_VERSION = "2"' collector/pricing.py
grep -n 'def diagnose_write' collector/collector.py
```

Check for the code, not a commit SHA — this runbook keeps getting doc-only
commits, so the tip SHA moves without the code changing. `ef64a65` (PR #16) is
where the code landed; anything at or after it is fine.

If the greps print nothing but `git pull` said "Already up to date", you are on
a branch or a detached HEAD. `git status` will say which; `git checkout main &&
git pull` fixes it.

- [X] Both greps print a match
- [X] Working tree updated, no local modifications lost

### 3. Rebuild the images and restart

`collector` and `awtrix-pusher` bake their `.py` files in at build time, so a
plain restart would keep running the old code. Grafana needs a restart because
its entrypoint copies the dashboard JSON into place at container start.

```sh
sudo docker compose up -d --build collector awtrix-pusher
sudo docker compose restart grafana
```

- [X] Both images rebuilt (build output shows the `COPY` layers re-running)
- [X] Grafana restarted

### 4. Confirm the collector is healthy again

```sh
sudo docker compose logs --tail=30 collector
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
sudo docker compose run --rm collector python -c "
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

Query result, 2026-07-28:

```
first 2026-07-17 08:34:41.191090+00:00
last  2026-07-28 20:37:15.040659+00:00
```

- [x] Range noted: `START = 2026-07-17`  `END = 2026-07-27` (END = yesterday;
      today is incomplete and has no full day to price)

Both scripts take **dates only** (`YYYY-MM-DD`) — they parse with
`date.fromisoformat`, which rejects a timestamp. The range is inclusive at both
ends, and the days are local NL days, not UTC.

**Expect `2026-07-17` to be excluded at step 7.** Collection started 08:34 UTC
= 10:34 CEST, so that day is missing its first ~10.5 hours — past both
`MAX_GAP_S` (20 min) and `MIN_COVERAGE` (0.98). Its prices are still worth
fetching in step 6, which does not care about sample coverage. That leaves
**10 complete days**, 07-18 → 07-27, as the realistic yield.

### 6. Ensure prices are complete for the whole range

```sh
sudo docker compose run --rm collector python prices.py --backfill 2026-07-17 2026-07-27
```

Watch for `No prices returned for …` lines — those days cannot be computed and
will be excluded in the next step. That is the correct outcome, not a failure.

Result, 2026-07-28: `Done: 264 price rows across 11 day(s)` — 24 rows for every
day, no gaps. 24 per day also confirms no DST transition in this range.

- [x] Completed
- [x] Any days reported as having no prices: **none**

So `price_coverage` will be exactly 1.0000 for all 11 days and the new gate
will exclude nothing on price grounds. That also means the existing version-1
rows were not understated — the recompute should reproduce the old numbers
rather than change them. The only expected exclusion remains 2026-07-17, on
sample coverage.

### 7. Recompute `daily_cost` at model_version 2

No `--force` needed: no version-2 row exists yet, so every day is reprocessed.

```sh
sudo docker compose run --rm collector python pricing.py --backfill 2026-07-17 2026-07-27
```

Each accepted day logs `wrote daily_cost`. Days that cannot be verified log
`EXCLUDED (price coverage … )` and are skipped — expected, and the whole point.

- [x] Completed
- [x] Count of days written: **10** (07-18 → 07-27)
- [x] Count of days excluded: **1** — 2026-07-17, `coverage 0.559 < 0.98`

Excluded on *sample* coverage, exactly as anticipated: collection began at
10:34 CEST that day. `price_coverage` was 1.000 on all 11 days, so the new gate
rejected nothing on price grounds, and `residual=0.000kWh` throughout — the
integration balances.

Ten-day total saving **€17.82**, ~€1.78/day.

#### Two things this run surfaced

**Max gap sits close to the limit on four days.** 07-18 (1052 s), 07-21
(1038 s), 07-23 (916 s), 07-27 (1047 s) against a `MAX_GAP_S` of 1200 s — about
87% of the threshold, clustered near the same duration rather than scattered.
That looks like a recurring daily event, not random API flakiness. These days
pass today, but a slightly longer outage drops one off the dashboard with no
warning beyond a log line. Worth reading `collector_health` for those dates to
find the `error_class`. Tracked as follow-up, not a blocker.

**Consumption changed sharply on 07-26.** Import/export goes from 0.31–1.14 /
0.43–4.41 kWh on 07-18…07-25 to 15.48/22.32 (07-26) and 17.96/19.06 (07-27) —
10–20× on both sides, with savings rising to €6.04 and €3.88. Benign if there
is a known cause; noted here so the step-9 comparison is not read as a model
artefact.

**07-19 saved −€0.07.** Not a fault: the no-battery counterfactual would have
exported 17.84 kWh into good prices, beating what storing it returned.

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
sudo docker compose run --rm collector python pricing.py --audit
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
sudo sh /volume1/docker/alphaess-collector/scripts/daily-savings.sh
```

- [ ] Runs clean, processes its 4-day window
- [ ] Re-running `pricing.py --audit` still reports `0 stale`

---

## Follow-ups this migration surfaced

Not blockers. Raised by the step-7 output, recorded so they are not lost.

1. **Recurring ~17-minute collection gap.** Four of ten days peaked at
   916–1052 s against a 1200 s limit. Find the cause before it crosses the
   threshold and starts silently excluding days:

   ```sh
   sudo docker compose run --rm collector python -c "
   import os
   from influxdb_client import InfluxDBClient
   c = InfluxDBClient(url=os.environ['INFLUX_URL'], token=os.environ['INFLUX_TOKEN'],
                      org=os.environ['INFLUX_ORG'])
   q = f'''from(bucket: \"{os.environ['INFLUX_BUCKET']}\")
     |> range(start: 2026-07-17T00:00:00Z)
     |> filter(fn: (r) => r._measurement == \"collector_health\")'''
   for t in c.query_api().query(q):
       for r in t.records:
           print(r.get_time(), r.values.get('event'), r.values.get('error_class'),
                 r.get_field(), r.get_value())
   "
   ```

   If the gaps cluster at the same wall-clock time, suspect something local (a
   NAS task, a container restart) rather than the AlphaESS API. Either way,
   raising `PRICING_MAX_GAP_S` is the wrong first move — it would hide the
   symptom.

2. **Step change in consumption on 2026-07-26.** Confirm there is a known
   cause, so the dashboard's totals are not read as a model artefact.

---

## Rollback

Nothing in this migration deletes or rewrites anything. The recompute only
*adds* version-2 rows beside the version-1 rows, which stay exactly as they
were. So rollback is putting the old code back, and the data follows.

**The commit to return to is `b4d500e`** — `main` immediately before this round
was merged (PR #15, "Collapse deployment to a single self-contained compose
file"). Confirm before relying on it:

```sh
cd /volume1/docker/alphaess-collector
git log --oneline b4d500e -1     # expect: Merge pull request #15 …
```

### Full rollback

```sh
cd /volume1/docker/alphaess-collector
git checkout b4d500e
sudo docker compose up -d --build collector awtrix-pusher
sudo docker compose restart grafana
```

`MODEL_VERSION` returns to `1` and the dashboard variable returns to `1` with
it — they move together, which is the whole reason they are pinned by a test.
Every panel reads the version-1 rows again and the numbers are what they were
before you started. The version-2 rows stay in the database, orphaned and
invisible. That is fine; leave them.

Note you are now on a detached HEAD. To get back onto the shipped code later:
`git checkout main && git pull`.

### Rolling back only part of it

The four commits are independent, so you can drop one without the others:

| Problem | Revert |
| ------- | ------ |
| Days you expected are missing from the dashboard | `git revert 136598d` (the pricing gate) |
| Collector misbehaving on failure | `git revert 7489457` (the fetch/write split) |

Then rebuild as above. Reverting the pricing commit also returns the dashboard
variable to `1`, since both live in that commit.

### If you want the version-2 rows gone

Not required — they cost a few kilobytes and nothing reads them after a
rollback. Only if you want a clean database, and **after** confirming the
backup from step 1 is good:

```sh
cd /volume1/docker/alphaess-collector
set -a; . ./.env; set +a

sudo docker compose exec influxdb influx delete \
  -t "$INFLUX_TOKEN" -o "$INFLUX_ORG" -b "$INFLUX_BUCKET" \
  --start 1970-01-01T00:00:00Z --stop $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --predicate '_measurement="daily_cost" AND model_version="2"'
```

`model_version` is a tag, so this predicate cannot touch version-1 rows or any
other measurement. It is still a delete — read it twice before running it.

### Restoring the backup

Only if something went wrong outside this runbook. Nothing in steps 0–11 needs
it.

```sh
sudo docker compose stop influxdb collector awtrix-pusher
sudo docker compose start influxdb
sudo docker compose exec influxdb influx restore /tmp/influx-backup \
  -t "$INFLUX_TOKEN" --full
sudo docker compose start collector awtrix-pusher
```

If you took the volume-tar fallback instead, restore that by stopping influxdb
and untarring over the volume — and mind the same downtime budget as step 1.

### After any rollback

`scripts/daily-savings.sh` runs nightly and will start writing version-1 rows
again for recent days. That is correct behaviour for the old code, not a
symptom. If you rolled back mid-migration and later roll forward, rerun step 7
for the affected range.

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
| 2026-07-28 | Steps 6 and 7 run and recorded: 264 price rows, 10 days written, 1 excluded (2026-07-17, sample coverage 0.559), €17.82 total. Added a Follow-ups section for the two things the output surfaced — a recurring ~17-minute collection gap sitting at 87% of `MAX_GAP_S`, and a 10–20× step change in import/export on 07-26. |
| 2026-07-28 | Step 1 marked done (backup taken hours earlier, still valid — nothing has written since). Step 2 gained a warning and an expected SHA: a pull from before PR #16 merged returns "Already up to date" while leaving the old code in place, which would make steps 3–7 look successful and write version-1 rows. |
| 2026-07-28 | Rollback expanded: names the exact commit to return to (`b4d500e`), adds per-commit partial reverts, an optional scoped delete of the version-2 rows, the restore procedure, and a note that the nightly job resumes writing version-1 rows afterwards. |
| 2026-07-28 | Every `docker` / `docker compose` command prefixed with `sudo`, and step 11's script invocation too — the NAS account is not in a docker group, so the runbook as written would have failed on the first command. |
| 2026-07-28 | Step 1: made explicit that `influx backup` is online and the volume-tar fallback is not. Added the downtime budget — writes are `SYNCHRONOUS` with no queue, so samples lost during a stop are permanent, and a gap over `MAX_GAP_S` (20 min) excludes that day from `daily_cost`. |
