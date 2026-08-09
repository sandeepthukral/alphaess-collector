# TODO: finish the efficiency rollout

Working notes for the conversion-loss feature (PR `conversion-loss-tracking`,
commit `ea44944`). Delete this file once everything below is done — it is a
rollout checklist, not permanent documentation. Anything here that turns out to
be permanently true belongs in DEPLOY.md instead.

Status as of 2026-08-06: code deployed on the NAS, 18 days backfilled, both
Kuma monitors green, gate proven to fire. PR not merged.

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

- [ ] **Falsify Monitor B.** Change `108000.0` to `1.0` in the Body, save,
      confirm the monitor goes red with `STALE`, change it back. A monitor never
      seen to fail is not known to work.
- [ ] **Rotate `INFLUX_TOKEN_KUMA`.** The current one was fully legible in a
      screenshot shared during setup. Read-only on `alphaess` and LAN-only, so
      the exposure is mild, but it is a live credential in an image.

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
- [ ] **Register the DSM task.** Task Scheduler → user-defined script, user
      `root`, daily **03:00** (after daily-savings at 02:00), command
      `/volume1/docker/alphaess-collector/scripts/daily-efficiency.sh`. Run it
      once from the DSM list — that exercises DSM's minimal environment, which
      is why the script carries its own `PATH` prelude.
- [ ] **Check the Grafana notification policy.** Alerting → Notification
      policies must point at a real contact point. Provisioned rules route
      through the default policy; if it points nowhere, every alert in this
      feature fires into silence. DEPLOY.md already records this trap.
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

## 4. `daily_savings` has no staleness check at all

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
