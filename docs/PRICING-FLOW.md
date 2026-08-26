# Pricing / cost pipeline flow

How day-ahead prices and 30s power samples become a day's cost, the battery's savings
(Model 1 vs. Model 2), and the quality gate and audit that keep `daily_cost` honest over time.
See `DESIGN-battery-savings.md` for the conceptual model.

## Fetching prices: `prices.py`

Quarter-hour reconstruction only applies to coarse (hourly) rows, only on/after the cutover
date, and only when explicitly requested — mixed cutover-boundary days are left alone.

```mermaid
flowchart TD
    A["fetch_prices_for_day(day)<br/>Frank Energie GraphQL"] --> B[rows]
    B --> C["split: fine (&lt;3600s) vs<br/>coarse (&gt;=3600s) rows"]
    C --> D{"day &gt;= CUTOVER_DATE (2026-08-01)<br/>AND coarse_rows present<br/>AND --reconstruct-if-coarse passed?"}
    D -- no --> KEEP["coarse rows stay as-is<br/>source = frank"]
    D -- yes --> E["fetch_quarter_hour_wholesale(day)<br/>EnergyZero 15-min day-ahead prices"]
    E --> F{wholesale prices fetched?}
    F -- no --> KEEP
    F -- yes --> G[reconstruct_quarter_hour_rows]
    G --> H{"per coarse row: contained<br/>EnergyZero quarters == round(duration/900)?"}
    H -- no --> KEEP
    H -- yes --> I["btw_ratio = market_price_tax / market_price<br/>per quarter: market_price = wholesale,<br/>tax = wholesale×btw_ratio, re-sum total<br/>source = frank+energyzero"]

    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef skip fill:#eceef0,stroke:#6b7280,color:#374151;
    class I ok;
    class KEEP skip;
```

## Integrating cost: `compute_day()` in `pricing.py`

Both models integrate the same 30s power samples against the same price intervals
(`integrate_by_interval` — trapezoidal, split at interval boundaries and sign zero-crossings);
they differ only in which signal stands for "grid flow."

```mermaid
flowchart LR
    SAMP["30s power samples:<br/>pv / grid / load / battery / soc"] --> INT["integrate_by_interval<br/>(trapezoidal, split at interval<br/>boundaries + zero-crossings)"]
    PRICE[price intervals] --> INT
    INT --> M1["Model 1 — actual<br/>signal = grid<br/>(real grid flow, battery in circuit)"]
    INT --> M2["Model 2 — counterfactual<br/>signal = grid + battery<br/>(as if the battery didn't exist)"]
    M1 --> COST1["cost_model1 += import_kwh×pi − export_kwh×pe"]
    M2 --> COST2["cost_model2 += import_kwh×pi − export_kwh×pe"]
    COST1 --> SAVE["saving = cost_model2 − cost_model1<br/>(the battery's value that day)"]
    COST2 --> SAVE

    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    class SAVE ok;
```

`pi` (import price) = `interval.total`. `pe` (export price, 2026 saldering) =
`market_price + market_price_tax + energy_tax` — the market price with the taxes refunded, and
no sourcing markup on either side of it. The markup is charged for sourcing energy, so it does
not arise on energy that was not sourced; `pi − pe` is therefore exactly one markup, not two.

## Quality gate: `gate()`

Checked in this order — price coverage before max-gap, deliberately: a partially-priced day is
*wrong*, not merely *thin*.

```mermaid
flowchart TD
    R[compute_day result] --> P1{"coverage &lt; MIN_COVERAGE (0.96)?"}
    P1 -- yes --> FAIL
    P1 -- no --> P2{"price_coverage &lt; 0.999?"}
    P2 -- yes --> FAIL["gate fails →<br/>EXCLUDED, nothing written<br/>(any existing row left untouched)"]
    P2 -- no --> P3{"max_gap_s &gt; MAX_GAP_S (1200s)?"}
    P3 -- yes --> FAIL
    P3 -- no --> PASS["gate passes →<br/>write daily_cost point<br/>tagged model_version"]

    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef fail fill:#faeaeb,stroke:#c14350,color:#a3323d;
    class PASS ok;
    class FAIL fail;
```

## Reconciliation: `audit_day()` / `run_audit()`

Catches rows written under old data or old rules that a plain rerun won't fix — `process_day`
silently leaves an already-written day untouched even if it would now fail the gate.

```mermaid
flowchart TD
    A["run_audit(): for each stored day<br/>(daily_cost rows in window)"] --> B{"row's model_version &lt;<br/>current MODEL_VERSION (3)?"}
    B -- yes --> SKIP["skip — superseded,<br/>not flagged"]
    B -- no --> C["reload samples + prices fresh<br/>audit_day(): recompute + gate()"]
    C --> D{"no samples / no intervals /<br/>gate() now fails?"}
    D -- yes --> STALE["stale — logged with a<br/>ready-to-run influx delete command<br/>scoped to model_version"]
    D -- no --> OK["ok — nothing to do"]

    classDef fail fill:#faeaeb,stroke:#c14350,color:#a3323d;
    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef skip fill:#eceef0,stroke:#6b7280,color:#374151;
    class STALE fail;
    class OK ok;
    class SKIP skip;
```

Nothing is deleted automatically — the operator runs the logged `influx delete` command, then
reruns `pricing.py` for those days.

## CLI entry points

| File | Flags | Effect |
|---|---|---|
| `prices.py` | `--date` \| `--backfill START END` (default: yesterday/today/tomorrow NL), `--dry-run`, `--reconstruct-if-coarse` | fetches and writes price intervals |
| `pricing.py` | `--date` \| `--backfill` (default: yesterday), `--csv PATH` (offline validation, implies dry-run), `--dry-run`, `--force` (bypass already-done skip), `--audit` (read-only; all stored days if no date given) | computes and writes `daily_cost`, or audits existing rows |

## File map

| File | Role |
|---|---|
| `collector/prices.py` | Fetches day-ahead prices; quarter-hour reconstruction for coarse rows. |
| `collector/pricing.py` | `compute_day()`, `gate()`, `process_day()`, `audit_day()`, `run_audit()` — integration, cost, gate, reconciliation. |
