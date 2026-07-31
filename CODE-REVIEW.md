# Code review — alphaess-collector

Date: 2026-07-28
Reviewer: senior staff engineering review (correctness, security, testing)
Scope: full repository at commit `cce4419` (branch `simplify-nas-deploy`), ~6.6k lines / 22 files.

> **Scope change, 2026-07-28.** A second project — a separate repository, *developed*
> on another machine but **deployed as its own container on this same NAS** — will
> write to this InfluxDB and render dashboards in this Grafana. That makes the stack
> *shared infrastructure* rather than one application's private backing store: it
> changes the verdict on three existing findings (#3, #5, #6) and adds five new ones
> (#10–#14), collected under
> [Shared infrastructure](#shared-infrastructure-second-project).
>
> The deployment topology is what decides most of it. Because both projects run on
> one Docker daemon, the second project reaches InfluxDB over the **internal Docker
> network** at `http://influxdb:8086` — not across the LAN. Nothing shared has to be
> published to a host port at all, except Grafana for your browser. #6's loopback
> recommendation therefore mostly *stands*, and the sharing is a lifecycle and
> namespacing problem (#14, #10, #11) rather than a network exposure one.

## Status

**Resume point, 2026-07-31.** Steps 0–6 of the revised order are done. #7 and
#9 are shipped but not yet applied to the NAS (code-only changes, take effect
on the next `git pull` + rebuild there). Next up is step 7 (#4, #8).

One operator action is outstanding and cannot be done from the repository: the
running Grafana still has the admin password it was initialised with, because
`GF_SECURITY_ADMIN_PASSWORD` binds only at first init. If it is weak, run the
reset in `DEPLOY.md`, "Changing the Grafana admin password". The `:?` guard from
#6 protects fresh installs only.

- [x] #1 Price-coverage gate — `priced_seconds()`, `price_coverage` field, `gate()` check
- [x] #2 Fetch/write failure-domain split — `stage` tracking + `diagnose_write()`
- [ ] #3 Hardcoded bucket in dashboards (48 refs across 4 dashboards + the alert rule) — independent of #12
- [ ] #4 `daily-savings.sh` date parsing
- [x] #5 Scoped InfluxDB tokens — PR #20, applied on the NAS 2026-07-29 (`MIGRATION-scoped-tokens.md`)
- [x] #6 Grafana password default — `:?` guard, no fallback ~~+ loopback binding~~ (binding half reversed, see #6)
- [x] #7 `sysSn` redaction in `error_summary` — `_URL_QUERY_RE`, strips the query string from any URL in the message
- [ ] #8 Flux parameter binding
- [x] #9 Log rotation (`x-logging` anchor, `max-size: 10m`/`max-file: 3` on all four services) +
      pinned `GF_INSTALL_PLUGINS` version — rest of #9 (`cap_drop`, `read_only`, `no-new-privileges`,
      memory limits) deliberately left, lower value per the ordering note below
- [x] Test scaffolding — pytest + ruff + GitHub Actions, 81 tests
- [x] Backoff cap vs. pricing gap gate — `MAX_BACKOFF_SECONDS`, PR #17 *(not from this
      review; found while investigating a recurring collection gap after the
      model_version 2 migration)*

**Shared infrastructure** (added 2026-07-28):

- [x] #10 Dashboards provisioned into the `AlphaESS` folder — PR #18, applied by PR #19
- [x] #11 Provisioning namespacing — `isDefault` dropped, per-project prefix table in `DEPLOY.md`
- [x] #12 Separate bucket for the second project — `planning`, 400d retention, created 2026-07-29
- [x] #13 Second project uses `http://influxdb:8086`, not the host port — `DEPLOY.md`, "Sharing the stack" (PR #18)
- [x] #14 `name: alphaess-net` + ownership/lifecycle note — PR #18
- [ ] Housekeeping — stale "capped at 5 minutes" in `DEPLOY.md` and the alert rule
- [x] Housekeeping — dashboard tags: already consistent (`alphaess` on all four), no change needed

### Notes on the completed items

**#7** — `error_summary` (`collector/collector.py`) now runs a redaction pass,
`_URL_QUERY_RE`, after the `(Caused by ...)` split and before truncation: any
`http(s)://` substring in the message has its query string stripped. This
covers the case the original finding was about — an `HTTPError` puts the full
request URL, `sysSn` and all, straight in `str(exc)` with no `(Caused by`
segment to hide it behind — without needing to special-case `HTTPError`
specifically, so it also catches a query string on any other exception type
that happens to embed one. `appId`/`appSecret` were already confirmed safe
(headers, never in the message); this only ever had `sysSn` to redact.

Verified with a new test,
`test_error_summary_redacts_the_query_string` in
`tests/test_collector_helpers.py`, asserting both the tag name (`sysSn`) and
the value (the serial number) are gone from the summary while the rest of the
message — including the URL path, which is useful for diagnosis — survives.
Not yet applied to the NAS; ships on the next deploy there.

**#9** — `docker-compose.yml` gained an `x-logging` anchor (`json-file`,
`max-size: 10m`, `max-file: 3`) applied via `logging: *default-logging` on
all four services. Default json-file logging has no cap of its own, and
three of the four services (collector, pusher, influxdb via the collector's
polling) log continuously forever — on a NAS now shared with a second
project, that's unbounded disk growth rather than a theoretical concern.
10m × 3 files × 4 services caps total log disk at 120 MB.

Also pinned `GF_INSTALL_PLUGINS` to `volkovlabs-echarts-panel@7.2.2` — it was
unpinned, so every fresh Grafana start pulled whatever the plugin registry's
current release was, a reproducibility and supply-chain gap. 7.2.2 is the
newest release that still supports Grafana 11.6.0 (the compose file's pinned
Grafana version); 7.2.4+ require Grafana ≥12.3.0. Bump both together if
Grafana is ever upgraded.

Left out, per the ordering note's "lower value" call: `cap_drop: [ALL]`,
`read_only: true`, `no-new-privileges`, memory limits. `docker compose
config` confirms the anchor resolves identically on all four services; not
yet applied to the NAS.

**#6 / #11** — the password half of #6 and all of #11, done together as step 3 of
the revised order. `GRAFANA_ADMIN_PASSWORD` lost its `:-admin` fallback for a `:?`
guard, `isDefault: true` is gone from the datasource, and `DEPLOY.md` gained the
per-project identifier table (datasource uid, provider name, mount path, folder,
dashboard uids, alert group/rule uid) that the second project needs.

Two things surfaced while doing it:

1. **`GF_SECURITY_ADMIN_PASSWORD` only applies when Grafana first initialises its
   database.** Changing `.env` and restarting an existing install does nothing and
   says nothing — the old password keeps working. So the guard protects fresh
   installs; changing the password later needs `grafana cli admin
   reset-admin-password`, now documented in `DEPLOY.md`. The live NAS instance
   therefore keeps whatever password it was created with until that is run.
2. **Dropping `isDefault` was free, and verified so rather than assumed.** Every
   query node in all four dashboards names `${DS_ALPHAESS}` or a Grafana built-in
   (`-- Grafana --`, `__expr__`), and the staleness rule names `datasourceUid:
   alphaess` — checked by `tests/test_grafana_provisioning.py`, which also pins
   the no-`isDefault` rule and the `AlphaESS` folder agreeing across the two
   provisioning files. The only behaviour change is that a panel created by hand
   in the UI starts with no datasource selected.

**Applied to the NAS 2026-07-29.** `isDefault: false` confirmed through
`/api/datasources`. A third deployment trap turned up in the process, and it is
the same shape as the two under #10 — a config change that reports success and
did nothing:

3. **`up -d` recreates nothing when the *resolved* config is unchanged.** The
   `:?` guard altered how `GRAFANA_ADMIN_PASSWORD` resolves, not what it
   resolves to, and provisioning files are bind-mounted — so Compose diffed an
   identical spec, printed `Container ... Running`, and left the old process
   holding the old datasource config. `restart grafana` is what applies a
   provisioning-only change; `Running` in that output means "did nothing", while
   `Started`/`Recreated` means it acted. Now a table plus a section in
   `DEPLOY.md` under "Updating", alongside the `networks:`/`volumes:` and
   dashboard-checksum cases.

   The pattern across all three: **this stack has no deployment mechanism that
   fails loudly when a change is not applied.** Verify the change at its
   destination — the API, the UI, the log — rather than trusting the deploy
   command's output.

**#5 / #12** — shipped in PR #20 and applied to the live stack on 2026-07-29 via
`MIGRATION-scoped-tokens.md`, which records the per-step results. Bucket `planning`
(400 d, ID `1430ea6bb66e9cb1`) alongside `alphaess` (`a83cd3d221d6111b`); four
scoped tokens; `INFLUX_TOKEN` now reaches only InfluxDB's own init. Cutover took
seconds, against the 20-minute `MAX_GAP_S` budget.

Three things worth remembering:

1. **Compose interpolates the whole file on *every* subcommand.** Once the `:?`
   guards landed, `docker compose exec` and even `ps` failed until the new
   variables existed — so the InfluxDB work has to run through plain `docker exec`,
   which needs no interpolation. The runbook was written the other way round and
   could not reach its own step 4.
2. **Verify tokens before cutting over, not after.** Scoping was confirmed from the
   `Permissions` column at creation, then each token exercised while the admin
   token was still live. The negative check — the pusher's write returning
   `403 insufficient permissions` — is the only one that catches a token broader
   than intended.
3. **Write-only was too tight for the planning token.** It was minted `w planning`
   as specified, then re-minted the same day as `rw planning` before deployment:
   without read on its own bucket, that project can neither skip runs it has already
   computed nor audit its own output. It still cannot write `alphaess`, which is the
   guarantee that actually matters here.

**#10 / #13 / #14** — shipped in PR #18, with PR #19 supplying the part that made
#10 take effect. Two Grafana behaviours cost a deployment round each and are worth
remembering before touching provisioning again:

1. Grafana **skips a provisioned dashboard whose checksum is unchanged**, and this
   repo's Grafana entrypoint regenerates the JSON byte-identically at every start.
   So a change to `dashboards.yml` alone — folder or anything else — is applied to
   new installs and silently ignored on existing ones.
2. A provisioned dashboard **cannot be deleted**, from the UI or the API, so
   delete-and-recreate is not available as a workaround. `disableDeletion` governs
   whether provisioning removes a dashboard when its file disappears, not whether
   an operator may remove one.

The lever is the file's bytes: bumping the top-level `"version"` in each dashboard
JSON changes the checksum, and a dashboard the provisioner actually processes is
written with the provider's current settings. Verified on the NAS.

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

---

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

> **Raised in priority by the second project (2026-07-28).** This was a
> configurability nit while one application owned the bucket. With a second
> project writing to the same InfluxDB, the two should be in *separate buckets*
> (see #12) — so "the bucket name is a literal in four dashboards and one alert
> rule" becomes the thing standing between you and tenant separation. Do this
> before, not after, the second project starts writing.

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

> **Promoted by the second project (2026-07-28).** The second project needs
> InfluxDB credentials. If it gets `INFLUX_TOKEN`, a container running code from
> a different repository — at a different stage of maturity, not covered by this
> review or these tests — holds a token that can drop the `alphaess` bucket and
> every day of history in it. There is no undo: this stack has no backup
> schedule, only the one-off taken during the model_version 2 migration.
>
> The token never leaves the NAS, so this is not about interception. It is about
> **blast radius**: scoped tokens are the only thing that would contain a bug or
> a mistaken `influx delete` on the other side of the boundary. **The second
> project must not receive `INFLUX_TOKEN`** — it should get a token that can
> write only its own bucket.

### 6. Grafana defaults to `admin`/`admin` and binds to all interfaces

`docker-compose.yml:95` — `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}`. If `.env`
is missing or that key unset, Grafana comes up with the default password on `0.0.0.0:3000`, holding
the token from #5. Same for InfluxDB on `0.0.0.0:8086`. On a NAS that's the entire LAN.

**Fix:** drop the `:-admin` fallback so a missing password fails loudly, and bind to loopback by
default (`127.0.0.1:${GRAFANA_PORT:-3000}:3000`) with a note in `DEPLOY.md` for users who genuinely
want LAN access.

> **Refined by the second project (2026-07-28).** Both halves survive, but they
> now apply to different services.
>
> **The password half stands, and matters more than before.** Grafana's admin
> account guards two projects' dashboards, and the datasource proxy behind it can
> issue arbitrary Flux against every bucket. Drop `:-admin`.
>
> **The binding half splits by service**, because the second project is a
> container on this same NAS and reaches InfluxDB over the Docker network at
> `http://influxdb:8086`. The published host port is not how it connects:
>
> | Service | Who needs the host port | Recommendation |
> | --- | --- | --- |
> | InfluxDB `:8086` | Nobody routinely. Both projects use the internal network; this repo's own admin work goes through `docker compose exec influxdb influx …`. | Bind `127.0.0.1:${INFLUX_PORT:-8086}:8086`. Keeps the UI reachable via an SSH tunnel when you want it, closed the rest of the time. |
> | Grafana `:3000` | Your browser, from another machine. | **Leave on the LAN.** This is the one genuine exposure, and an SSH tunnel is not a reasonable ask for routine dashboard viewing. |
>
> So the original recommendation was right about InfluxDB and wrong about Grafana.
> Grafana is then the single LAN-facing surface, holding a token that can read and
> write everything (#5) behind a password that currently defaults to `admin` —
> which is precisely why the password half is the more urgent of the two.

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

## Shared infrastructure (second project)

Added 2026-07-28. A separate repository — developed on another machine, deployed as
its own container on this same NAS — will write to this InfluxDB and render
dashboards in this Grafana.

The stack was built on an assumption that is no longer true: that one application
owns it. Nothing here is a bug today — every item is a collision that appears the
first time something else provisions into the same Grafana, writes to the same
InfluxDB, or comes up beside these containers. They are cheap to prevent now and
awkward to unpick afterwards, because by then both sides are live and holding data.

**Decide first, because everything below depends on it: this repository owns the
stack.** `docker-compose.yml` here defines InfluxDB, Grafana, their volumes, and
`alphaess-net`. The second project declares all of them `external` and defines none
of its own. Write that down in `DEPLOY.md` in both repositories — the failure mode
is not dramatic, it is two databases quietly holding half the data each.

### 10. Dashboards are provisioned into the root, not into a folder

`grafana/provisioning/dashboards/dashboards.yml:5` sets `folder: ""`, so all four
AlphaESS dashboards land in the top-level Dashboards list.

The `AlphaESS` folder visible in the UI was **not** created by that provider — it
exists because `grafana/provisioning/alerting/alphaess-staleness.yml:25` declares
`folder: AlphaESS`, and Grafana creates the folder named by an alert rule. So the
folder holds the alert rule and nothing else, while the dashboards sit loose beside
it. That is confusing with one project and unusable with two: the root list becomes
an unsorted mix of both projects' dashboards, with no way to tell which belongs to
what.

**Fix:** set `folder: AlphaESS` in `dashboards.yml`, matching the folder the alert
rule already creates. One line, and it is reproducible — a folder created by hand
in the UI is lost on any rebuild that starts from an empty Grafana volume, whereas
a provisioned one is not.

**Setting it is not sufficient on an existing install** — corrected 2026-07-28,
after the change shipped and the dashboards stayed in the root. Grafana skips a
provisioned dashboard whose checksum is unchanged, and the Grafana entrypoint
rewrites the JSON byte-identically on every start, so the files look untouched and
the new folder is never applied to dashboards that already exist. Fresh installs
are fine; upgrades are not, and nothing is logged. Nor can the dashboards simply be
deleted and recreated — Grafana refuses to delete a provisioned dashboard from the
UI or the API. Bumping the top-level `"version"` in each JSON changes the checksum
and is what actually applies the folder (`DEPLOY.md`, "If the pull changed the
dashboard folder").

Two further caveats: `allowUiUpdates: true` is set, so a dashboard saved from the
UI has drifted from the file and recreation will discard those edits. And the
folder is matched by *title*, so it must read exactly `AlphaESS` in both files.

The second project should provision its own dashboards into its own named folder,
by the same mechanism.

### 11. Grafana provisioning has no namespacing, and collisions are silent

Every provisioning identifier in this repo is either generic or unqualified. Each
one is a collision waiting for the second project to declare the same thing:

| Identifier | Where | What a collision does |
| --- | --- | --- |
| Datasource `name: alphaess`, `uid: alphaess` | `datasources/influxdb.yml:4-5` | Same uid from another file: one definition silently wins, and which one depends on file load order. |
| `isDefault: true` | `datasources/influxdb.yml:8` | Two defaults: Grafana picks one. Any panel that relies on "default" rather than naming its datasource then reads the **wrong database** and shows plausible, wrong numbers. |
| Provider `name: alphaess` | `dashboards/dashboards.yml:4` | Provider names must be unique; a duplicate makes Grafana drop one provider and its dashboards never appear. |
| Provider path `/var/lib/grafana/dashboards` | `dashboards/dashboards.yml:8` | If the second project mounts into the same directory, both providers claim the same files and each deletes dashboards it does not recognise. |
| Dashboard UIDs | the four `grafana/*.json` | A duplicate UID overwrites the other project's dashboard. |
| Alert group `alphaess-health`, rule uid `alphaess-data-stale` | `alerting/alphaess-staleness.yml:23,29` | Same uid replaces the rule; the displaced alert stops evaluating, silently. |

The dangerous property they share: **none of these fail loudly.** The failure is a
dashboard that quietly vanishes, or one that renders the wrong project's data.

**Fix:** the pattern for a shared Grafana is one provider, one folder, one mount
path, and one uid prefix per project. This repo is already close — the names are
just unqualified rather than wrong. Concretely: keep `alphaess` as the prefix here,
document that the second project must use its own throughout, and drop
`isDefault: true` so that no panel anywhere can depend on which datasource happens
to be default. Every dashboard in this repo already references `${DS_ALPHAESS}`
explicitly, so removing the default flag costs nothing.

### 12. Bucket separation, and the token that goes with it

The two projects should write to separate InfluxDB buckets. Same reasoning as
separate folders — independent retention, independent tokens, and one project's
`influx delete` typo cannot reach the other's history.

This is the concrete form of #5, and the two must be done together: separate
buckets are only a boundary if the tokens are scoped to them. The token handed to
the second project should be **write-only on its own bucket**, and if its
dashboards live in this Grafana, a **read-only** token for the datasource.

**Fix order:** #12 (create the second bucket) → #5 (mint scoped tokens) → hand the
second project only its own.

**#3 is not a prerequisite** — corrected 2026-07-28, having earlier claimed it was.
Templating the bucket lets *this* repo follow its own `INFLUX_BUCKET` setting. The
second project gets a different bucket by `influx bucket create` and a token scoped
to it; these dashboards go on reading `alphaess` regardless. The two are
independent, and sequencing #3 first would put the slowest item in front of the one
that actually blocks handing over credentials.

#3 remains worth doing on its own merits — a `INFLUX_BUCKET` set to anything else
today blanks every panel and makes the staleness rule fire permanently.

### 13. Keep InfluxDB traffic on the Docker network, not the host port

InfluxDB authenticates with a long-lived bearer token in a plain HTTP header. On
the internal bridge network that is fine — the traffic never leaves the host.

The risk is the second project being configured the easy way, with
`INFLUX_URL=http://<nas-ip>:8086` (the address you would naturally reach for while
developing on another machine) instead of `http://influxdb:8086`. That works, so
nothing complains, and it quietly puts an admin-capable token on the LAN in
cleartext on every write — permanently, because it is in a `.env` nobody revisits.

**Fix:** the second project joins `alphaess-net` (#14) and uses the service name.
Say so explicitly in its `DEPLOY.md`, with the reason, because the host-IP form is
the one that will otherwise get copied from a development machine into production.

With that done, the only LAN-facing surface is Grafana (#6), and #13 needs no
further work.

### 14. Two compose projects, one set of shared services

The second project is a separate `docker-compose.yml` in a separate directory on
the same Docker daemon. Compose isolates projects by default, which is what makes
the sharing work — and also what makes each of these silently *not* work:

- **The network name is generated.** `alphaess-net` has no `name:` key, so Docker
  creates it as `alphaess-collector_alphaess-net` — derived from this repo's
  directory name. The second project must join it as `external`, which means
  hardcoding a name that changes if this directory is ever renamed. **Add an
  explicit `name: alphaess-net`** so the identifier is stable and intentional.

- **MTU is inherited, and only if it joins.** `driver_opts` caps the bridge at
  1400 to stop large TLS handshakes being dropped on Synology uplinks — the root
  cause of a real outage here. A container joining `alphaess-net` gets that fix for
  free. One that creates its own network gets the default 1500 and is exposed to
  the same failure, which presents as intermittent
  `SSL: UNEXPECTED_EOF_WHILE_READING` against whatever external API it calls, days
  apart, with nothing pointing at the network. If the second project talks to an
  external HTTPS API, this is the highest-value thing to tell its author.

  Note the existing caveat in `docker-compose.yml`: `driver_opts` only apply at
  network *creation*. If the second project ever declares the network non-external
  and Docker recreates it, the cap is silently lost.

- **No `depends_on` across projects.** The second project cannot wait for InfluxDB
  to be healthy. It needs `restart: unless-stopped` and code that tolerates
  InfluxDB being absent at startup — the same discipline the collector already has.

- **Lifecycle coupling runs one way and is easy to forget.** `docker compose down`
  in *this* directory stops InfluxDB and Grafana out from under the second project,
  which will keep running and failing. `docker compose down -v` destroys both
  projects' data, and the `driver_opts` comment already instructs a `down`/`up`
  cycle for MTU changes — a documented procedure that now has a side effect on
  something the runbook never mentions.

**Fix:** state in `DEPLOY.md` that this repository owns InfluxDB, Grafana, and
`alphaess-net`; that the second project declares all three as external and adds
none of its own; and that `down -v` here is destructive to both. Add `name:
alphaess-net`. Two compose files each defining an `influxdb` service on one host is
the outcome to design against — it ends in two databases, two volumes, and data
split across them.

---

## Housekeeping

Small, unrelated to the findings above.

- **`folder: AlphaESS` in `dashboards.yml`** — the fix from #10, and the item that
  prompted this section. Also confirm all four dashboards carry consistent `tags`,
  so they stay findable by tag once a second project's dashboards share the list.
- **Stale backoff references.** PR #17 lowered the backoff cap from 300 s to a
  configurable `MAX_BACKOFF_SECONDS` (default 120 s). Two comments still say
  "capped at 5 minutes": `DEPLOY.md:195` and
  `grafana/provisioning/alerting/alphaess-staleness.yml:3-4`. Both describe *why*
  the staleness alert exists, so the wrong number undermines the reasoning rather
  than just being untidy.

  Worth noting while editing them: the staleness threshold is 300 s and is
  unrelated to the backoff cap. With the cap at 120 s the two no longer coincide,
  which is an improvement — a single backoff sleep can no longer be as long as the
  alert window, so the alert firing now unambiguously means "several failed polls"
  rather than possibly "one long sleep".

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

~~1. Price-coverage gate (#1)~~ — done.
~~2. Diagnosis/InfluxDB conflation (#2)~~ — done.
~~4. Test scaffolding~~ — done.

**Revised 2026-07-28**, for the remaining items. The ordering principle has
changed: it is no longer "worst bug first" but **"what gets harder once the second
project is live"**. Anything touching shared identifiers is cheap now and expensive
after both sides depend on the current names.

0. **#14 `name: alphaess-net`** — one line, and it must exist *before* the second
   project writes its compose file, because that file will hardcode whatever the
   network is called. Getting this wrong is the only item here that forces edits in
   the other repository later.
1. **#10 dashboard folder** — one line, immediate benefit, and it establishes the
   folder-per-project convention before there is a second project to argue with.
2. **#12 separate bucket** → **#5 scoped tokens**. The only chain that blocks
   handing the second project a credential, and short: `influx bucket create`, two
   `influx auth create` calls, and a `.env` change on each side. Do not give it
   `INFLUX_TOKEN` in the meantime.

   (**#3 is *not* part of this chain** — see #12. It was listed as a prerequisite
   here until 2026-07-28; it isn't one.)
3. **#6 password fallback** and **#11 `isDefault` / namespacing** — small, and both
   are about removing ways for the two projects to interfere. The password one is
   now the more urgent half of #6, since Grafana stays the one LAN-facing service.
   *Done 2026-07-29; see the notes below, including the one caveat — the password
   guard binds only at first init, so the running instance is unchanged.*
4. **The `DEPLOY.md` ownership note** (#13, #14) — cheap, and it is what stops the
   second project being pointed at `http://<nas-ip>:8086` or declaring its own
   `influxdb`. Worth doing early precisely because it costs nothing. *Done in PR
   #18 — "Sharing the stack with another project" in `DEPLOY.md` covers ownership,
   the service-name URL, and the `down -v` warning. Nothing left here; #11 later
   added the per-project identifier table to the same section.*
5. **#7 `sysSn` redaction** — unchanged in priority; a leak to a third-party
   monitor, and self-contained. *Done 2026-07-31; see the notes below. Not yet
   applied to the NAS.*
6. **#9 log rotation** — unbounded disk on a NAS now filling from two projects.
   The rest of #9 (`cap_drop`, `read_only`) is lower value. *Done 2026-07-31; see
   the notes below. Not yet applied to the NAS.*
7. **#4 `daily-savings.sh` parsing**, **#8 Flux binding** — neither is currently
   reachable by an attacker or a realistic input; do them when convenient.

**#6's InfluxDB loopback binding is deliberately *not* in this list.** It is right,
but it should land only once the second project is confirmed to be joining
`alphaess-net` and using `http://influxdb:8086`. Doing it first would break that
project the moment it was configured the other way — and the resulting error would
send its author looking in the wrong repository.
