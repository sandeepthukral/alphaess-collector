# Code review — alphaess-collector

Date: 2026-07-28
Reviewer: senior staff engineering review (correctness, security, testing)
Scope: full repository at commit `cce4419` (branch `simplify-nas-deploy`), ~6.6k lines / 22 files.

## Overall

The engineering quality here is above average for a homelab project — the comments explain *why*
rather than *what*, failure modes are thought through (backoff, diagnosis, heartbeat semantics),
and the MTU/TLS incident is documented at the point of use.

The problems are concentrated in three places:

1. A silent correctness bug in the pricing model.
2. A monitoring path that reports the wrong verdict.
3. Zero tests on code that plainly needs them.

---

## Correctness

### 1. Missing price intervals silently halve the computed cost — and the quality gate passes it

`collector/pricing.py:135` drops energy that falls outside any known price interval
(`if idx is None: continue`). `collector/pricing.py:183` computes `coverage` purely from *sample*
timestamps, so it never notices that prices were missing. `process_day` only guards against
`intervals` being completely empty.

Confirmed by running `compute_day` with a synthetic day of constant 1000 W import:

| Prices available | `cost_model1` | `coverage` | gate |
| ---------------- | ------------- | ---------- | ---- |
| 24 h (correct)   | €2.4000       | 1.0000     | ok   |
| 12 h             | **€1.2000**   | **1.0000** | **ok** |

The day gets written to `daily_cost` with half the true cost and a perfect quality score. It then
passes `_already_done` and is never recomputed.

This is the most damaging bug in the repo, because `daily-savings.sh` runs a rolling 4-day window
whose entire purpose is self-healing against *late-published prices* — the exact scenario that
produces partial interval coverage.

**Fix:** compute price coverage alongside sample coverage and gate on it.

```python
priced_s = sum((iv["till"] - iv["from"]).total_seconds()
               for iv in intervals
               if iv["till"] > win_start and iv["from"] < win_end)
result["price_coverage"] = round(priced_s / day_len, 4)
```

Reject in `gate()` when it is below ~0.999. Deriving `day_len` from `day_window_utc` (as
`compute_day` already does) keeps DST days (23/25 h) correct.

### 2. An InfluxDB outage is diagnosed and alerted as "AlphaESS's fault, nothing to fix"

In `collector/collector.py:399-423`, `write_api.write()` is inside the same `try` as the API fetch.
With `SYNCHRONOUS` write options, a write failure raises. So if InfluxDB is down:

- `consecutive_failures` climbs.
- At 3, `diagnose_network` probes DNS for `openapi.alphaess.com` and `https://1.1.1.1/` — both
  succeed, since neither has anything to do with InfluxDB.
- Verdict is `upstream`, and the phone alert says *"the fault is at the AlphaESS API. Nothing to fix
  here — the collector keeps retrying and resumes on its own."*

That is the opposite of the truth, on the one alert intended to tell you whether to get out of bed.
The staleness alert's own comment already recognises that "InfluxDB refusing writes" is a distinct
cause; the diagnosis doesn't.

**Fix:** separate the two failure domains — catch the write in its own `try`, tag the health event
with a `stage` (`fetch` / `write`), and skip `diagnose_network` for write failures.

### 3. Dashboards and the alert rule hardcode `bucket: "alphaess"`

All four dashboards plus `grafana/provisioning/alerting/alphaess-staleness.yml` embed the literal
bucket name, but `INFLUX_BUCKET` is configurable throughout compose and documented in
`.env.example`. Set it to anything else and every panel goes blank while the collector writes
happily — and `noDataState: Alerting` means the staleness rule fires permanently. The alert file at
least carries a "change the bucket here" comment; the dashboards don't.

**Fix:** use a Grafana constant/template variable (`${bucket}`) resolved by the same `sed` pass the
entrypoint already runs for `${DS_ALPHAESS}`.

### 4. `daily-savings.sh` date parsing is fragile

`scripts/daily-savings.sh:31-32`: `echo "$DATES" | awk '{print $1}'` prints field 1 of *every* line.
Any extra stdout line from `docker compose run` makes `START` two words, and `set -eu` won't catch
it — you'd get a confusing failure inside `prices.py`.

**Fix:** use `awk 'NR==1{print $1}'` / `NR==1{print $2}`, and validate the result matches
`^[0-9]{4}-[0-9]{2}-[0-9]{2}$` before use.

---

## Security

### 5. One all-powerful token shared by every component

`docker-compose.yml:12` sets `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN: ${INFLUX_TOKEN}`, and the same value
is handed to the collector, the AWTRIX pusher, and Grafana. That token can create and drop buckets,
mint other tokens, and delete data. The collector needs write-only on one bucket; the pusher and
Grafana need read-only.

Grafana is the sharp edge: anyone who reaches its UI can issue arbitrary Flux through the datasource
proxy with org-admin credentials.

**Fix:** create scoped tokens after `influx setup` and use those. At minimum, don't give the admin
token to Grafana.

### 6. Grafana defaults to `admin`/`admin` and binds to all interfaces

`docker-compose.yml:95` — `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}`. If `.env`
is missing or that key unset, Grafana comes up with the default password on `0.0.0.0:3000`, holding
the token from #5. Same for InfluxDB on `0.0.0.0:8086`. On a NAS that's the entire LAN.

**Fix:** drop the `:-admin` fallback so a missing password fails loudly, and bind to loopback by
default (`127.0.0.1:${GRAFANA_PORT:-3000}:3000`) with a note in `DEPLOY.md` for users who genuinely
want LAN access.

### 7. `sysSn` leaks into the heartbeat URL and logs

Verified by running `error_summary`. For an `HTTPError` there's no `(Caused by ...)` segment to
strip, so the full URL survives:

```
HTTPError: 401 Client Error: Unauthorized for url:
https://openapi.alphaess.com/api/getLastPowerData?sysSn=AL5006148000012345
```

That string becomes the `msg=` query parameter pushed to Uptime Kuma (`send_heartbeat`), which may
be a hosted third party. Connection errors happen to be safe because the `(Caused by` split discards
the URL — but that's incidental, not intentional.

**Fix:** redact the query string in `error_summary` before it goes anywhere.

`appId`/`appSecret` are in headers and don't leak — that part is right.

### 8. Flux built by string interpolation

`collector/pricing.py:285`, `collector/pricing.py:409`, `awtrix-pusher/pusher.py:77`. All inputs are
operator-set env vars, so this is not currently exploitable — flagged as a standards issue rather
than a live vulnerability. A `sys_sn` containing `"` would produce a broken or altered query.

**Fix:** use the client's `params=` binding.

### 9. Container hardening gaps

Non-root users are set — good. Missing:

- No `cap_drop: [ALL]`, no `read_only: true`, no `no-new-privileges`, no memory limits.
- **No log rotation.** On a NAS with default json-file logging and a service that logs every 30 s
  indefinitely, that's an unbounded disk consumer. Add a `logging` block with `max-size`/`max-file`.
- `GF_INSTALL_PLUGINS: volkovlabs-echarts-panel` is unpinned and fetched from the internet on every
  start — both a reproducibility and a supply-chain concern. Pin the version.

---

## Testing

**There are no tests, no test framework, no linter config, and no CI.** For this codebase that's the
single highest-leverage gap, because the hard-to-verify logic is all *pure functions with no I/O* —
the easiest possible thing to test:

| Function | Why it needs tests |
| -------- | ------------------ |
| `_accumulate` | Zero-crossing split, sign conventions. The `f = ps/(ps-pe)` branch is where silent euro errors live. |
| `integrate_by_interval` | Boundary cuts, samples outside intervals (finding #1), DST-length days |
| `compute_day` | Coverage arithmetic, gap detection, residual |
| `export_price` | Encodes a saldering policy decision; a regression here is money |
| `error_summary` | `(Caused by` split, truncation, redaction (finding #7) |
| `parse_fields` | Partial/missing/non-numeric API fields |
| `fmt_power`, `soc_color`, `build_apps` | Pure formatting, trivially testable |

`load_samples_csv` already exists as a seam for feeding fixture data — the offline `--csv` path is
effectively a manual test harness that was never turned into an automated one.

**Suggested:** `pytest` + a `pyproject.toml` with `ruff`, a `tests/` directory with a golden-day
fixture (CSV + canned price JSON) asserting exact euro figures, and a GitHub Actions workflow
running lint + tests. Property-based tests on `_accumulate` (via `hypothesis`) would pay for
themselves — assert that the integral of a ramp equals `import_wh - export_wh` for arbitrary
`ps`/`pe`.

---

## Code quality (smaller items)

- **`env()` calls `sys.exit(1)`** (`collector.py:63`, duplicated in all three modules). A lookup
  helper shouldn't terminate the process, and it makes the function untestable. Raise a
  `ConfigError` and exit in `main()`. The triplication itself should be a shared module.
- **`int(env(...))` / `float(os.environ[...])` are unguarded** — `pricing.py:46-51` reads config at
  *import time*, so a typo in `PRICING_MAX_GAP_S` produces a bare `ValueError` traceback and makes
  the module impossible to import in a test without setting env vars. Move to a `load_config()`
  function with validation.
- **No connection reuse.** Every poll opens a fresh TLS connection (`collector.py:221`). Given that
  oversized TLS *handshake* packets were the root cause of the whole MTU saga, a module-level
  `requests.Session` would cut handshakes by ~30x and directly reduce exposure to that failure mode.
  Pair it with a `urllib3.Retry` adapter.
- **Busy-wait sleep loops** — `while running and time.monotonic() < deadline: time.sleep(1)` in both
  services. `threading.Event().wait(timeout)` gives instant, exact shutdown instead of up to 1 s of
  latency plus needless wakeups.
- **Slow shutdown**: SIGTERM only sets a flag, so a poll blocked in a 30 s request delays exit past
  Docker's 10 s grace period → SIGKILL. Either lower the timeout or set `stop_grace_period: 40s`.
- **Pusher's `query_latest` has no `sys_sn` filter** (`pusher.py:77`) and constructs a new
  `query_api()` every cycle.
- **`fmt_power(999.6)` returns `"1000W"`** rather than `"1.0kW"` — cosmetic, but it's a boundary a
  test would catch.
- **No `.dockerignore`** in either build context.

---

## Suggested order

1. Price-coverage gate (#1) — it's silently corrupting the data you're collecting the data *for*.
2. Diagnosis/InfluxDB conflation (#2) — makes the alerting actively misleading.
3. Grafana password default + loopback binding (#6) and scoped tokens (#5).
4. Test scaffolding + tests for `_accumulate` / `integrate_by_interval` / `compute_day` — these lock
   in #1 and prevent the next one.
5. Hardcoded bucket (#3), log rotation (#9), `sysSn` redaction (#7).

---

## Status

- [x] #1 Price-coverage gate — `priced_seconds()`, `price_coverage` field, `gate()` check
- [x] #2 Fetch/write failure-domain split — `stage` tracking + `diagnose_write()`
- [ ] #3 Hardcoded bucket in dashboards
- [ ] #4 `daily-savings.sh` date parsing
- [ ] #5 Scoped InfluxDB tokens
- [ ] #6 Grafana password default + loopback binding
- [ ] #7 `sysSn` redaction in `error_summary`
- [ ] #8 Flux parameter binding
- [ ] #9 Container hardening / log rotation
- [x] Test scaffolding — pytest + ruff + GitHub Actions, 81 tests

### Notes on the completed items

**#1** — `price_coverage` is a new field on the `daily_cost` measurement, gated at
`PRICING_MIN_PRICE_COVERAGE` (default 0.999). Days with incomplete prices are now *excluded* rather
than written with an understated cost; the rolling window in `scripts/daily-savings.sh` picks them
up on a later run once the day-ahead prices land. Documented in `DESIGN-battery-savings.md`.

Verified against the pre-fix code: a day priced for 12 of 24 hours produced `cost_model1 = 1.20`
against a true 2.40, with `coverage = 1.000`, and the old gate returned `(True, 'ok')`.

Shipped with `MODEL_VERSION = "2"` and the dashboard's **Model version** variable moved to match
(the two are now pinned together by `tests/test_model_version_consistency.py` — a mismatch shows
stale numbers rather than an empty dashboard, so it fails silently). Recomputing history at
version 2 republishes every verifiable day and omits the rest, orphaning the unverifiable version-1
rows instead of requiring them to be found and deleted. Confirmed by differential run: a fully
priced day computes bit-identically at versions 1 and 2, on 23-, 24- and 25-hour days.

`pricing.py --audit` (read-only) re-checks stored days at the current model version against today's
gate, for the case where the underlying data changed after a row was written.

**#2** — the loop now tracks which half of the poll is in flight. `diagnose_network` runs only for
fetch failures; write failures get `diagnose_write`, which returns `local-influxdb` and points at
the container/token/disk instead of claiming the AlphaESS API is at fault. The stage is also a tag
on `collector_health` failure events and a prefix on the heartbeat message
(`write: ConnectionError: ... [local-influxdb]`), so it is visible from the first failure rather
than only from the third.

Verified by reverting the conditional: `test_influxdb_write_failure_is_not_blamed_on_alphaess`
fails with `diagnose_network_calls == 1`.

**Test scaffolding** — `pyproject.toml` (pytest + ruff config), `requirements-dev.txt`,
`tests/`, `.github/workflows/ci.yml`. `pythonpath` puts `collector/` and `awtrix-pusher/` on the
path so tests import the same flat modules the Dockerfiles produce. Two ruff rules (`B905`,
`RUF007`) are disabled with rationale — they would force a mechanical rewrite of the
`zip(xs, xs[1:])` integration code, and `strict=` is wrong there by construction.

Remaining known lint/quality debt is listed under "Code quality" above and is untouched.
