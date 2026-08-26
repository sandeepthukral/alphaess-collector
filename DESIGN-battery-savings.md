# Battery savings analysis — design

Status: **implementation in progress.** `prices.py` is built and validated
against the live API; `pricing.py` is built and pending validation against real
`power_readings`. The Grafana dashboard and daily scheduling are still to do.
This document remains the spec.

## Goal

Quantify what the home battery is worth in euros, by pricing the same measured
household against two worlds:

- **Model 1 — with battery (actual).** Price the energy that actually flowed
  through the grid meter. Used to **validate** our accounting against the real
  Frank Energie bill.
- **Model 2 — without battery (counterfactual).** Assume the battery was never
  installed; whatever it charged would have been exported, whatever it
  discharged would have been imported. Price that.

**Battery value = cost(Model 2) − cost(Model 1)**, per day and over any
selected period. A day can show a *loss* (round-trip losses / poor timing
outweigh arbitrage); the sign is kept.

## Data we have

Measurement `power_readings` in bucket `alphaess`, tag `sys_sn`, sampled every
30 s by `collector.py`:

| Field | Unit | Sign convention |
|---|---|---|
| `pv_power_w` | W | solar generation (≥0) |
| `grid_power_w` | W | **+ = import, − = export** |
| `load_power_w` | W | house load (≥0) — **derived, not measured**; see "Where the losses come from" |
| `battery_power_w` | W | **+ = discharge, − = charge** |
| `soc_percent` | % | battery state of charge |

Sign conventions are the AlphaESS API defaults (`pgrid`, `pbat`); **verify with
`collector.py --once` before trusting any euro figure** — every result below
inverts if a sign is flipped.

## The accounting (why the counterfactual is nearly free)

At the AC bus, every instant obeys:

```
load = pv + grid + battery          (grid: +import/−export, battery: +discharge/−charge)
```

Remove the battery (`battery := 0`) with `load` and `pv` unchanged:

```
grid_cf = load − pv = grid_actual + battery_power_w
```

So the no-battery grid series is just **the actual grid plus the battery power
added back**. Both models then run through the *same* pricing engine; only the
grid series differs.

Consequences worth stating:

- **Round-trip efficiency is already captured.** `battery_power_w` is measured
  at the AC bus, so charge-Wh naturally exceed discharge-Wh over time. The
  counterfactual correctly credits *not* wasting that ~10–15% loss. No
  efficiency fudge factor.
- **Grid arbitrage is captured for free.** If the battery grid-charges on cheap
  intervals and discharges on expensive ones, that shows up automatically in
  `grid_cf` vs `grid_actual`.
- **Balance closure is a data-quality gate.** Model 2 is only as true as
  `pv + grid + battery ≈ load` in the hardware. We compute the daily residual
  and store it; large-residual days are flagged/excluded (also catches sign
  mistakes early).

## Pricing model

### Atomic slot = Frank's price interval

Frank Energie billed at **hourly** granularity through 2026-07-31 (the hourly
price was the average of the four 15-min EPEX values). **From 2026-08-01 the
contract moves to 15-minute settlement**, billed per slot directly rather than
as an hourly average. We do **not** hardcode 15 vs 60 min: `prices.py` stores
whatever `from`/`till` intervals the API returns, and `pricing.py` integrates
power within those exact boundaries. This cutover needed no code change —
verified 2026-07-31 by a full-repo audit (see `CODE-REVIEW.md`) and new tests
at 15-minute resolution alongside the existing hourly ones
(`tests/conftest.py`'s `quarter_hour_intervals`).

### Fallback if Frank's API doesn't actually cut over

`marketPricesElectricity` is a public, unauthenticated GraphQL endpoint — it
has no idea it's "us" or that our contract moves to 15-min settlement on
2026-08-01. It's plausible it keeps returning 24 hourly rows/day indefinitely
even after our real bill is settled per quarter, if it's a generic display
feed decoupled from the actual billing engine. That case is dangerous
precisely *because* it's silent: `price_coverage`/`gate()` only checks that
priced seconds cover the day, and a stale hourly price repeated across 4
quarters still covers 100% of it — nothing would look wrong while every
quarter's cost is computed from the wrong sub-hour rate.

Verified empirically (2026-07-31, real data for 2026-07-29) that a correct
fallback exists: Frank's hourly `marketPrice` is the plain average of the
real quarter-hour day-ahead wholesale prices published by EnergyZero's public
API (`public.api.energyzero.nl/public/v1/prices?...&interval=INTERVAL_QUARTER`
— no auth, the same feed the sibling battery-planning repo already uses,
sourced from the NL day-ahead auction which has cleared in 15-minute MTUs
since 2025-10-01). Max difference across a full day: 0.00001 €/kWh. Frank's
other components don't scale with the market price the same way:
`sourcing_markup` and `energy_tax` are flat €/kWh across all rows in a day,
while `market_price_tax` is exactly 21% BTW of `market_price`. So the correct
per-quarter reconstruction is:

```
quarter.market_price      = EnergyZero's real quarter-hour wholesale price
quarter.market_price_tax  = quarter.market_price × (hourly_row.market_price_tax / hourly_row.market_price)
quarter.sourcing_markup   = hourly_row.sourcing_markup      (unchanged)
quarter.energy_tax        = hourly_row.energy_tax           (unchanged)
```

Implemented in `collector/prices.py` as `fetch_quarter_hour_wholesale()` +
`reconstruct_quarter_hour_rows()`, wired in behind an opt-in
`--reconstruct-if-coarse` flag — **not automatic**. We don't yet know for
certain that Frank's real 15-min billing actually varies within the hour
(rather than some suppliers literally re-billing every quarter at the same
repeated hourly-average rate); the EnergyZero match tells us the correct
number *if* it varies, not that it does. Silently swapping in a plausible-
looking reconstructed number unvalidated against a real invoice would
recreate the exact "confidently wrong" failure this fallback exists to
avoid — see `CODE-REVIEW.md`. Reconstructed points are tagged
`source=frank+energyzero` (vs. `source=frank`) for auditability;
`pricing.py` doesn't filter or group on that tag, so this needed no changes
there. `prices.py`'s `run()` always logs a warning the moment a
post-cutover day comes back coarse, whether or not the flag is set.

### Per-slot netting is *exact* for 2026 (not an approximation)

Frank confirmed that **before 2027 saldering nets in full** — energy tax and BTW
are refunded on returned electricity. Combined with our assumption that
**annual export ≤ import** (we have <1 year of data, so we net everything):

- **Commodity (EPEX ± markup):** a dynamic contract already prices each interval
  at its own rate. Per-slot *is* the mechanism. Exact.
- **Energy tax + BTW:** the rate is *flat* across slots, so
  `Σ (import_i − export_i) × tax = (Σimport − Σexport) × tax`. Per-slot netting
  equals the legally-correct annual volume netting. Exact.

  This argument depends only on the tax rate being flat across slots, not on
  how long a slot is — it holds identically whether a slot is one hour or 15
  minutes. "Per-slot netting" is the accurate framing across the 2026-08-01
  granularity cutover, not "per-hour."

Therefore daily results are **additive** — a day is self-contained *and* the
annual total is right. Period stats are plain sums of daily rows. (This
exactness **ends in 2027** when tax netting on export is removed — see Open
items.)

### Prices come from Frank's API, per component

`marketPricesElectricity` (endpoint `https://frank-graphql-prod.graphcdn.app/`,
public GraphQL, no auth, confirmed live) returns per interval:

```
total = marketPrice + marketPriceTax + sourcingMarkupPrice + energyTaxPrice
```

The components are BTW-handled per-part: `marketPriceTax` is the 21% BTW on the
market price, and `sourcingMarkupPrice` / `energyTaxPrice` are themselves
BTW-inclusive. So `total` is the fully all-in consumption price (€/kWh). One
call returns one Amsterdam local day — 23/24/25 hourly rows across DST through
2026-07-31, ×4 (92/96/100 rows) from 2026-08-01 under 15-minute settlement.

We store all four components + the all-in `total` + `from`/`till`. This means:

- Model 1 matches the bill **to the cent** using Frank's own numbers.
- Rate changes (annual energy-tax updates, the 2027 saldering cliff) are tracked
  automatically — no hardcoded €0.0175 markup or €0.09161 tax.
- Every euro is decomposable for audit.

Reference figures for 2026 (informational — the API is the source of truth; live
values observed on 2026-07-18): `sourcingMarkupPrice` ≈ €0.01815/kWh (incl. BTW),
`energyTaxPrice` ≈ €0.11085/kWh (incl. BTW, = €0.09161 excl. × 1.21); BTW 21%;
fixed delivery €4.99/mo; tax credit €628.96/yr (fixed costs cancel in the
Model 2 − Model 1 difference).

### Import vs export price

Per slot *i*, with `import_i`/`export_i` the integrated grid energy (kWh):

```
cost_actual_i = import_actual_i · p_import_i − export_actual_i · p_export_i
cost_cf_i     = import_cf_i     · p_import_i − export_cf_i     · p_export_i
saving_i      = cost_cf_i − cost_actual_i
```

- `p_import_i` = Frank's all-in `total` for the slot.
- `p_export_i` (salded, 2026) — **implemented as option (b)**; still to be pinned
  against a real teruglevering bill line after 2026-07-26:
  - (a) Frank's `feedIn` price field directly — **not available**: the field
    fails validation on the public GraphQL endpoint and introspection is
    disabled, so the API gives the four components and nothing about what they
    pay you.
  - (b) `marketPrice + marketPriceTax − sourcingMarkupPrice + energyTaxPrice` —
    implemented until 2026-08-26 and **wrong**. Deducting the markup claims
    Frank levies a feed-in fee of the same size. Nothing supports that.
  - (c) **(implemented)** `marketPrice + marketPriceTax + energyTaxPrice` — the
    markup is simply absent. Frank: "Wanneer je stroom teruglevert, ontvang je
    daarom de marktprijs die op dat moment geldt", with the energy tax and BTW
    refunded. The markup is charged for sourcing energy on your behalf; on
    export there is nothing to source.
  - We **exclude the ~15% teruglever bonus** (it applies to specific cases only).

### The surprise this model will show

The flat energy-tax refund is decoupled from the slot price and, under 2026
saldering, is refunded on export. So even at a **negative** EPEX slot, exported
energy is still worth **positive** money (tax refund dominates). Saldering
already rescues midday exports, so the battery's 2026 benefit is **much smaller**
than a raw-EPEX view suggests — its value is mostly the commodity day/night
spread plus the ±markup, not tax arbitrage. This flips 2027.

## Architecture

Nothing runs in Grafana/Flux — the per-slot pricing and price join are too much
for a datasource query, and results are cached. Two new Python entrypoints
alongside `collector.py`, writing pre-computed rows Grafana only reads and sums.

```
collector.py   raw 30s power_readings                         (exists)
prices.py      daily fetch Frank marketPricesElectricity  →  market_price   (NEW)
pricing.py     per complete day: Model 1/2               →  daily_cost      (NEW)
dashboard      read + sum daily_cost                                        (NEW)
```

### Time & timezone convention

**Everything is stored as true UTC instants; "days" are an Amsterdam-local
concept applied only at read/compute time.** This is the standard InfluxDB model
and is DST-safe, but it means a day *looks* like it starts the night before when
you inspect raw timestamps in UTC.

- **What's stored:** `market_price` points at each interval's `from` (a UTC
  instant); `daily_cost` points at local-midnight-expressed-as-UTC
  (`day_window_utc()` in `pricing.py`, via
  `datetime.combine(day, time(), NL_TZ).astimezone(utc)` — zoneinfo resolves the
  real offset per date, so no hardcoded `+01:00`/`+02:00`).
- **Local midnight in UTC:** `22:00` the previous day in summer (CEST, UTC+2),
  `23:00` the previous day in winter (CET, UTC+1). So in a UTC-based view a day's
  first row shows at 22:00 or 23:00 "the night before" — this is **correct
  storage, not a bug.**
- **How to view it right:** the InfluxDB Data Explorer and all Grafana panels
  render in Amsterdam/browser time, so days line up on `00:00` there. Every
  day-truncating Flux query uses `import "timezone"` +
  `option location = timezone.location(name: "Europe/Amsterdam")` with
  `today()`/`date.sub`; the dashboards set `"timezone": "browser"`. Only a raw
  UTC query (or the Data Explorer with a UTC range) makes the previous-night
  offset visible.
- **Day boundaries in code** (`pricing.py` windows, coverage length,
  `daily-savings.sh` date window) all go through `Europe/Amsterdam` zoneinfo, so
  23 h and 25 h DST-transition days are handled correctly.

### `prices.py`

- POST GraphQL to Frank's endpoint (no auth); fetch electricity market prices
  for the target day(s).
- Write measurement **`market_price`**, tag `sys_sn` unnecessary (prices are
  system-independent — tag by nothing or a constant `market` source), one point
  per interval timestamped at its `from`:
  fields `market_price`, `market_price_tax`, `sourcing_markup`, `energy_tax`,
  `total`, `feed_in` (if used), `duration_s` (till − from).
- Run daily after prices are final (day-ahead prices are known the day before,
  so the previous complete day is always final). `--backfill FROM TO` for
  history (subject to how far back the API serves).

### `pricing.py`

For each **complete** day not already processed at the current `model_version`:

1. Pull `power_readings` and `market_price` for the local day
   (Europe/Amsterdam boundaries, DST-aware).
2. Integrate `power × real_Δt` (trapezoidal) within each price interval, capping
   any single Δt (a long outage must not hold a stale power value across a slot).
3. Compute `import/export` for actual and counterfactual, price each slot, sum.
4. Write measurement **`daily_cost`**, tag `sys_sn`, timestamp = local midnight:

| Field | Meaning |
|---|---|
| `cost_model1` | with-battery cost (€) |
| `cost_model2` | no-battery cost (€) |
| `saving` | `cost_model2 − cost_model1` (signed) |
| `import_kwh_actual`, `export_kwh_actual` | grid energy, actual |
| `import_kwh_cf`, `export_kwh_cf` | grid energy, counterfactual |
| `load_kwh` | total house consumption, priced hours only (v3+) |
| `delta_soc_percent` | SoC at 24:00 − 00:00 (borrow/bank indicator) |
| `delta_soc_kwh` | same in kWh — only if `BATTERY_CAPACITY_KWH` is configured |
| `coverage` | fraction of expected (DST-aware) samples present |
| `price_coverage` | fraction of the local day covered by price intervals |
| `max_gap_s` | longest sample gap |
| `sample_count`, `span_s` | raw sample diagnostics |
| `balance_residual_kwh` | ∫‖pv+grid+battery−load‖ dt (quality) |
| `billed_cost` | actual Frank daily cost (optional, entered later for validation) |
| `model_version` | tag; schema/model version for cache invalidation |

### Complete-day / missing-data policy

- **Coverage is time-based, not count-based.** Real polling drifts (~30.8 s, not
  exactly 30 s) and would fail a naive count/2880 test, so coverage measures
  *missing time*: `1 − (un-sampled head/tail + Σ gaps beyond 3× the poll
  interval) / local-day length` (DST-aware, 23/25 h days handled). Normal cadence
  and a skipped poll or two never count as missing.
- Process a day only if **coverage ≥ 96%** *and* **max single gap ≤ 20 min**.
  Scattered misses barely move an integral (linear interpolation self-corrects);
  one long contiguous gap distorts a specific price slot, so it's gated
  separately. Both thresholds are configurable (`PRICING_MIN_COVERAGE`,
  `PRICING_MAX_GAP_S`).
- **Price coverage is gated separately, and near-absolutely (≥ 99.9%,
  `PRICING_MIN_PRICE_COVERAGE`).** Sample coverage and price coverage fail
  independently, and only the first is visible in the samples: energy landing in
  a slot with no price row is *discarded* by the integration, not
  approximated. A day priced for only half its slots (12 of 24 hourly, or 48 of
  96 quarter-hourly from 2026-08-01) therefore costs out at exactly half the
  true figure in both models — with `coverage` still reading
  1.000 — and once written it is never revisited (see Caching / idempotency).
  Unlike sample coverage there is no interpolation to lean on, so the threshold
  admits float error on the boundary arithmetic and nothing else. The rolling
  window in `scripts/daily-savings.sh` makes this self-healing: a day excluded
  today because the day-ahead auction had not published is simply recomputed on
  a later run.

### Caching / idempotency

- `pricing.py` skips days that already have a `daily_cost` row **at the current
  `model_version`**; bumping the version reprocesses.
- The dashboard reads one version at a time (its **Model version** variable), so
  a bump must be mirrored in `grafana/alphaess-battery-savings.json` or every
  panel keeps showing the previous version's rows — stale numbers rather than an
  empty dashboard, which is why `tests/test_model_version_consistency.py` pins
  the two together.
- Rows at superseded versions are left in place rather than deleted: they cost
  one point per day, they are invisible while the dashboard is set to the
  current version, and they are the only record of what the earlier model
  claimed. **Version 2** added the price-coverage gate; a day present at 1 but
  absent at 2 is one whose prices could not be verified as complete.
- Idempotent writes (overwrite the day's point), so re-runs are safe.
- Never cache a day whose prices were still provisional (won't happen for the
  previous complete day, but guard `--backfill` against missing price intervals).

## Dashboard (new, read-only over `daily_cost`)

- **Table** — one row per processed day: date, Model 1, Model 2, signed saving,
  (optional) billed cost + Model-1 delta for validation, coverage/quality flag.
- **To-date stats** (fixed all-time range override): total saving, total days
  analysed, total kWh shifted, effective €/kWh the battery earned.
- **Selected-period stats** (follow the dashboard time picker): same four,
  summed over the range — trivial because rows are additive.
- **Daily saving bars + ΔSoC overlay** so cross-midnight borrow/bank days are
  visible and the day-to-day jitter is explained.

Note: "days analysed" (not "days with battery") — both models run on every clean
day; there is no with/without split in the day set.

## Open items / risks

1. ~~**Export price (a) vs (b)** — pin against a real teruglevering bill line.~~
   **Closed 2026-08-26**: neither. Option (c) — the markup is absent, not
   deducted. Found by reconciling the Battery Savings dashboard against
   `report_day.py`'s own savings line, which disagreed by ~8% for a week and by
   exactly `2 × sourcing_markup × exported_kWh` per day, to the cent. The
   planner priced export at the full import price (one markup too high) and this
   model at import minus two markups (one too low); the truth sat between them.
   A bill line is still worth checking for the teruglever bonus, which both
   sides exclude.
2. **Sign-convention verification** — run `collector.py --once`; confirm
   `pv+grid+battery−load ≈ 0` on real samples before trusting euros.
3. **Frank API backfill depth** — confirm how far back `marketPricesElectricity`
   serves; older days may need an alternative source or may be unrecoverable.
4. **DST correctness** — day windows and price-slot alignment in Europe/Amsterdam
   including the 23 h / 25 h transition days. Handled via zoneinfo throughout;
   see [Time & timezone convention](#time--timezone-convention) for the
   UTC-storage / local-day model (and why a UTC view shows a day starting the
   night before).
5. **2027 saldering cliff** — tax netting on export ends; the "per-slot netting
   is exact" property breaks and export tax handling must change. The
   per-component price storage means the price side tracks automatically, but the
   *netting rule* in `pricing.py` will need a date-aware branch.
6. **Balance residual threshold** — choose the cutoff that flags/excludes a day.

## Assumptions (recap)

- Annual export ≤ import → net all returned energy (we have <1 year of data).
- 2026 saldering in full (tax + BTW refunded on export).
- Load and PV are identical between the two worlds (removing the battery changes
  nothing else in the house).
- The ~15% teruglever bonus is excluded (specific-case only).

---

## Where the losses come from (added 2026-08-06)

The `balance_residual_kwh` metric above turned out to be structurally incapable
of doing what it was built for, and the reason is worth recording rather than
rediscovering.

**`load_power_w` is an identity, not a measurement.**
`load_power_w == pv_power_w + grid_power_w + battery_power_w` holds *exactly* on
every sample — all 56,969 of them checked over 2026-07-17 → 2026-08-06, maximum
absolute residual **0 W**. AlphaESS's `getLastPowerData` derives house load as
the residual rather than metering it. Two consequences:

- The AC energy balance in "The accounting" above closes by construction.
  `balance_residual_kwh` is identically zero and can never flag anything.
- `load_power_w` goes negative (−7847 W observed), which no house can do.

So conversion and standby losses are invisible in `power_readings`, and no
amount of integrating it changes that.

### The two losses, measured separately

Only the battery's own loss is visible in the 30-second data, via the asymmetry
between charge and discharge over a window whose start and end SoC match. Over
2026-07-17 → 2026-08-06, start and end SoC both 14.8%: **373.97 kWh in, 359.58
kWh out — 14.39 kWh lost, 96.15% round-trip**, corroborated by eight
non-overlapping SoC-matched sub-windows (pooled 95.93%).

The rest needed a second, independent load figure, which
`getOneDayPowerBySn` provides at 5-minute resolution. Its `load` is *not* the
residual: on 2026-08-05 21:03 NL it reported 571 W where the identity gave 723 W,
across four consecutive records, while the same payload's SoC matched
`power_readings` exactly and its `feedIn` matched grid export to 1.4%. That gives

```
conversion + standby = Σ(derived load − metered load)      getOneDayPowerBySn
battery internal     = Σ(charge) − Σ(discharge) − ΔSoC·C   getOneDateEnergyBySn
total                = the two added
```

**The two terms do not double-count.** `eCharge`/`eDischarge` reproduce this
repo's own integration of `battery_power_w` to within 1.4% over 19 local days
(charge 364.20 vs 360.10 kWh, discharge 352.60 vs 347.58; out/in 96.81% vs
96.52%), so they measure the same plane, and the conversion term is measured
entirely outside it.

Measured over 2026-07-18 → 2026-08-05: conversion + standby **1.1–2.7 kWh/day**,
battery internal **~0.7 kWh/day**.

### Two data defects the module has to survive

1. **`ppv` in `getOneDayPowerBySn` is always 0.0** on this system — three full
   days summed to 0.00 kWh while `getOneDateEnergyBySn` reported 17–25 kWh of PV
   for the same days. Not stored; see `efficiency.series_points`.
2. **`load` in the same endpoint intermittently returns a wildly wrong single
   value** — 5832 W where the 30-second series says ~500 W. 26 records in 5430
   (0.48%) across 19 days, worth 8.7 kWh of phantom load, with 14 of them on
   2026-08-01 alone: enough to take that day's conversion loss to **−4.59 kWh**,
   inverting the sign of the quantity being measured. The filter tests each
   record against the derived series rather than against its neighbours, because
   that is the physically meaningful direction — conversion loss makes derived ≥
   metered, always — and because a genuine appliance spike appears in both series
   and must survive. Dropped records are still stored in `metered_power`; the
   filter applies to the integral, not to the archive.

### Schema

Write measurement **`metered_power`**, tags `sys_sn` + `source`, timestamp = the
record's own `uploadTime` converted from naive Amsterdam local to UTC (fold-aware
in October, gap-dropping in March — see `efficiency.parse_upload_times`).

Write measurement **`daily_energy`**, tags `sys_sn` + `model_version`, timestamp
= local midnight expressed as UTC, the same convention as `daily_cost`.

One field there is not about energy: **`computed_at_unix`**, the wall-clock
instant the row was written. Every staleness check reads it, because a
`daily_energy` row's own timestamp is 51 hours old on a healthy system right
before the next nightly run — a check on that would need a >51 h threshold and
would take two and a half days to notice a dead job.

### What still cannot be measured

Whether `eCharge`/`eDischarge` are metered at the battery's DC terminals or
AC-referred is not documented anywhere and no endpoint distinguishes them. If
they are AC-referred, some conversion loss is counted in both terms. The 1.4%
agreement with `battery_power_w` says only that the two agree with *each other*.
Settling it needs a physical AC meter on the inverter, not another API call.
