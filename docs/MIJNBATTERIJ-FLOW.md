# mijnbatterij.nl submission flow

How one InfluxDB read becomes one public leaderboard row: what is refused, what is
sent, and which two fields are guesses about an undocumented API rather than
measurements. See DEPLOY.md, "Publishing to mijnbatterij.nl" for the operator's
side of this.

The service (`collector/mijnbatterij.py`) reads only InfluxDB. It never calls the
AlphaESS API, so it cannot perturb collection and adds nothing to the shared
`appId` rate budget.

## One submission cycle: `collect()` then `submit()`

Two refusals, both silent-failure guards rather than errors: a stale sample would
be published as live, and a missing price feed would be published as a real
€0.00 rather than an unknown one.

```mermaid
flowchart TD
    A["every MIJNBATTERIJ_INTERVAL_SECONDS (300)"] --> B["today_window(now)<br/>local NL midnight → now"]
    B --> C["pricing.load_samples_influx<br/>power_readings"]
    C --> D{any samples?}
    D -- no --> SKIP1["log, write mijnbatterij_submit<br/>outcome=no-data, submit nothing"]
    D -- yes --> E{"age of newest sample<br/>&gt; MIJNBATTERIJ_STALE_AFTER_SECONDS (600)?"}
    E -- yes --> SKIP2["submit nothing<br/>(a collector outage is not a reading)"]
    E -- no --> F["battery_energy_kwh(samples)<br/>chargedToday / dischargedToday"]
    F --> G["pricing.load_prices_influx<br/>market_price"]
    G --> H{any price intervals?}
    H -- no --> Z["batteryResult = 0<br/>+ warning"]
    H -- yes --> I["pricing.compute_day(...)['saving']<br/>gate() deliberately NOT applied"]
    Z --> J
    I --> J["Totals.get() — cached MIJNBATTERIJ_TOTALS_TTL_SECONDS<br/>Σ daily_cost.saving, Σ daily_energy.discharge_kwh_api"]
    J --> K["build_payload()"]
    K --> L["POST /api/live<br/>Bearer MIJNBATTERIJ_API_KEY"]
    L --> M{status}
    M -- "2xx" --> OK["mijnbatterij_submit outcome=ok<br/>heartbeat up"]
    M -- "4xx / 5xx / transport" --> ERR["SubmitError<br/>outcome=rejected|unreachable<br/>body logged (the only schema doc there is)<br/>heartbeat down from the 2nd in a row<br/>backoff ×2, capped at 3600 s"]

    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef skip fill:#eceef0,stroke:#6b7280,color:#374151;
    classDef bad fill:#fdecec,stroke:#c0392b,color:#8b2620;
    class OK ok;
    class SKIP1,SKIP2,Z skip;
    class ERR bad;
```

## Where each field comes from

| Payload field | Source | Note |
|---|---|---|
| `timestamp` | newest `power_readings` sample | the measurement's time, never the submission's |
| `batteryCharge` | `soc_percent` | |
| `batteryPower` | `battery_power_w` | **sign flipped by default.** AlphaESS `pbat` is + while discharging; sent charge-positive unless `MIJNBATTERIJ_CHARGE_POSITIVE=0` |
| `chargedToday` / `dischargedToday` | `battery_power_w` integrated from local midnight | via `pricing._accumulate`, so an interval that crosses zero lands in both totals instead of netting |
| `batteryResult` | `pricing.compute_day()['saving']` over today so far | the same two-world model `daily_cost` stores, ungated |
| `batteryResultTotal` | Σ stored `daily_cost.saving` at the current `model_version`, + today | |
| `totalBatteryCycles` | (Σ `daily_energy.discharge_kwh_api` + today) / `BATTERY_CAPACITY_KWH` + `MIJNBATTERIJ_CYCLES_OFFSET` | throughput-equivalent, not an event count |
| `mode` | `MIJNBATTERIJ_MODE` | a setting: the platform's bucketing of a DIY dispatcher is unverifiable from here |
| `loadBalancingActive` | `MIJNBATTERIJ_LOAD_BALANCING` | nothing in this stack does load balancing |

## Why `gate()` is not applied to today

`pricing.gate()` decides whether a day is trustworthy enough to **store
permanently** in `daily_cost`, and by construction a partial day fails its
coverage check — so applying it here would mean never submitting a euro figure at
all.

The bargain is different for a live value: today's number self-corrects on the
next cycle, five minutes later, and is superseded outright by the gated
`daily_cost` row tomorrow night. It is the same *model* either way, which is what
matters — a separate intraday model would make the platform's daily total step at
midnight by the difference between the two.

## Two known under-counts in `batteryResultTotal`

Both are deliberate: the total under-states rather than estimating.

- A day rejected by `pricing.gate()` is absent from `daily_cost` forever, and so
  from this sum. Filling it with an estimate would put a number on a public
  leaderboard that no stored row can ever be reconciled against.
- Yesterday is missing until `daily-savings.sh` runs (~02:00), so the total dips
  for the first couple of hours of each local day and then recovers.
