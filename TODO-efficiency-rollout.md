# TODO: finish the efficiency rollout

Working notes for the conversion-loss feature (PR `conversion-loss-tracking`,
commit `ea44944`). Delete this file once everything below is done — it is a
rollout checklist, not permanent documentation. Anything here that turns out to
be permanently true belongs in DEPLOY.md instead.

Status as of 2026-08-09: code deployed on the NAS, 18 days backfilled, both
Kuma monitors green, gate proven to fire, Monitor B proven to fail. Item 1 is
done in the repo; the token rotation is declined. PR #56 still open — what is
left before merging is the DSM task and the notification policy below.

---

## 1. Fix the Kuma recipe in DEPLOY.md (blocks merge) — DONE 2026-08-09

Applied. Two notes on what changed versus what is written below: the
`localhost` correction turned out to be unnecessary — DEPLOY.md already said
`http://<nas-host>:8086/...` — and the raw Flux is kept in the doc as a
readable form for pasting into `influx query`, with the JSON envelope as the
thing Kuma is actually given. Retries `1` was genuinely missing and is added.

`## Monitoring the nightly efficiency job` documents Monitor B as a raw Flux
body with `Content-Type: application/vnd.flux`. **Uptime Kuma rejects that** —
its Body field validates against the Body Encoding dropdown, which is JSON, and
raw Flux is not JSON. Following the doc as written costs an hour and ends in a
400.

The working configuration, confirmed on the NAS:

- Body Encoding stays **JSON**
- `Content-Type: application/json`
- Body is InfluxDB's JSON query envelope:

  ```json
  {"query":"from(bucket: \"alphaess\") |> range(start: -14d) |> filter(fn: (r) => r._measurement == \"daily_energy\" and r._field == \"computed_at_unix\") |> max() |> map(fn: (r) => ({_value: if float(v: uint(v: now())) / 1000000000.0 - r._value < 108000.0 then \"FRESH\" else \"STALE\"})) |> yield(name: \"freshness\")","type":"flux"}
  ```

Mention `application/vnd.flux` only as the fallback it is: it works if Body
Encoding is switched to **XML**, which skips validation. The JSON form is
preferable because it does not misdescribe its own payload.

Two more corrections while in there:

- The URL must be the NAS's LAN address (`http://192.168.68.105:8086/...`), not
  `localhost`. Kuma runs outside this compose stack, so `localhost` inside its
  container is Kuma itself.
- Retries `1`, not `0`. At 0 a single blip pages you.

## 2. Remaining rollout steps on the NAS

- [x] **Falsify Monitor B.** Done 2026-08-09: `108000.0` → `1.0` took the
      monitor red with `STALE`, reverted. The monitor is now known to fail as
      well as to pass.
- [~] **Rotate `INFLUX_TOKEN_KUMA`** — **declined 2026-08-09**, risk accepted.
      The token was fully legible in a screenshot shared during setup. It is
      read-only on `alphaess` and reachable only from the LAN, so the exposure
      is mild; the judgement is that it is not worth the rotation. Left here
      rather than deleted so the decision is visible: if the NAS is ever exposed
      beyond the LAN, or the screenshot travels, this becomes live again.
      Commands kept for that day.

      ```sh
      cd /volume1/docker/alphaess-collector
      INFLUX_TOKEN=$(grep -E '^INFLUX_TOKEN=' .env | cut -d= -f2- | tr -d '"')
      ALPHAESS_ID=$(sudo docker compose exec -T influxdb influx bucket list \
        -t "$INFLUX_TOKEN" -o home --name alphaess --hide-headers | awk '{print $1}')
      sudo docker compose exec -T influxdb influx auth create \
        -t "$INFLUX_TOKEN" -o home -d "uptime-kuma: r alphaess (v2)" \
        --read-bucket "$ALPHAESS_ID"
      ```

      Put the new token in the monitor, confirm green, then
      `influx auth list` / `influx auth delete -i <id>` the old one. Delete only
      the row described `uptime-kuma: r alphaess` — the list also holds the admin
      token and the collector's rw token.
- [x] **Register the DSM task.** Done — registered at 03:00 as documented, and
      it has run four consecutive nights.
- [x] **Check the Grafana notification policy.** Done 2026-08-09: the default
      policy delivers to a **Telegram Bot** contact point, reusing the channel
      Uptime Kuma already notifies through, and its Test delivered. There are no
      child policies, so the tree is a single node and every alert instance
      reaches it regardless of labels — nothing to preview or match. Timings
      left at the defaults (30s group wait, 5m group interval, 4h repeat), which
      suit a once-nightly job. Both provisioned rules show up under Alerting →
      Alert rules in Normal state, confirming the files were picked up.

      Not provisioned, though: `grafana/provisioning/` covers datasources,
      dashboards and rules, but neither contact points nor policies. This
      routing lives only in Grafana's SQLite inside the `alphaess-grafana-data`
      volume, which `backup-influxdb.sh` does not reach — lose that volume and
      the rules come back while the routing silently does not.
- [ ] **Merge the PR** once item 1 is pushed.

## 3. Check the DST fold by hand on 2026-10-26

The `uploadTime` timezone is assumed, not documented by AlphaESS. The
SoC-alignment gate is supposed to catch a mis-parse — but it currently reads
**0.00pp**, exactly zero, so it cannot be exercised by lowering its threshold.
Its only real proof is a live DST transition.

On 2026-10-26, run:

```sh
cd /volume1/docker/alphaess-collector && sudo docker compose run --rm collector \
  python efficiency.py --dry-run --date 2026-10-25
```

Expect **300** records over a 25-hour day and `soc_align` still near zero. If
alignment jumps to tens of pp, the fold handling is wrong and the day will be
gated rather than silently mis-integrated — which is the intended failure, but
it needs fixing rather than ignoring.

---

## 4. `daily_savings` has no staleness check at all — DONE 2026-08-09

Applied in the repo, with two deliberate departures from what is written below:

- `computed_at_unix` is set on the `Point` in `process_day`, not added to
  `compute_day`'s `result`. `compute_day` is re-run by `audit_day` to judge
  rows already stored, and a wall-clock field would make it return something
  different every call for identical inputs.
- The monitor is **not** filtered on `model_version`, reversing step 3 below.
  `max(computed_at_unix)` across all versions is exactly the liveness question;
  filtering would read STALE for the whole of any post-bump backfill. This
  matches the "Job age" panel's exemption, which
  `tests/test_model_version_consistency.py` already pins.

`MODEL_VERSION` stays at `3` — the field takes no part in the arithmetic, and
bumping would have hidden every existing savings row until a full backfill ran.
The reasoning is recorded in `pricing.py`'s version comment so it does not read
as an oversight.

Step 4 (a provisioned Grafana alert and a Job age stat on the savings
dashboard) was scoped out, not done.

**Still to do on the NAS:** create the Kuma monitor, minding the deploy
ordering below — the field only appears on rows written after this deploys.



The nightly `daily-savings.sh` produces the money figure, and it can stop
writing with no symptom other than the savings dashboard quietly going flat.
Nothing watches it. Same four failure modes as the efficiency job, including the
one that exits 0.

**Correction to what I said earlier:** I described this as "Monitor B verbatim
with `daily_energy` → `daily_cost` and `computed_at_unix` → its equivalent."
There is no equivalent. `pricing.py` writes only the fields in its `result`
dict, and none of them records when the job ran — `daily_cost` rows are stamped
at the local midnight of the day they describe, so the newest row is ~51 h old
on a healthy system just before the next run. A staleness check against the row
timestamp would need a threshold above 51 h and would take two and a half days
to notice a dead job. That is the exact problem `computed_at_unix` was added to
`daily_energy` to solve.

So this is a small code change, not a config change:

1. In `pricing.py`, add `computed_at_unix` to the daily point, mirroring
   `efficiency.py`. Note the field is written from `result`, so either add it to
   that dict or set it explicitly on the `Point` after the loop — the loop
   coerces every value with `float()`, which is fine for an epoch second.
2. Extend `tests/test_pricing_*.py` with the assertion `efficiency` already
   carries: every written `daily_cost` row has `computed_at_unix`.
3. Add a second Kuma keyword monitor, same JSON-envelope shape as item 1, with
   `_measurement == "daily_cost"`, plus a `model_version` filter (which
   `daily_energy` does not need in its monitor but `daily_cost` does — it has
   real version history).
4. Optionally mirror the Grafana staleness alert as
   `alphaess-savings-staleness.yml`, and add a job-age stat to the savings
   dashboard with its threshold pinned by a test, as the efficiency one is.

**Deploy ordering gotcha:** existing `daily_cost` rows have no
`computed_at_unix`, so `max()` over them returns nothing and the monitor reads
STALE until the first post-deploy run writes the field. Either run `pricing.py`
by hand immediately after deploying, or create the monitor the following day.
Do not "fix" this by widening the range.

Threshold: `daily-savings.sh` runs at 02:00, so 30 h (`108000.0`) is right here
too — one missed night alerts, an hour's slip does not.
