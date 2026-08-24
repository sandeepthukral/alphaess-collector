# Efficiency reconciliation flow

How `collector/efficiency.py` turns a day's raw AlphaESS history into a trusted
`daily_energy` row — or decides the day isn't trustworthy enough to write.

## Per-day orchestration: `run_influx()` / `process_day()`

```mermaid
flowchart TD
    START["run_influx(days, dry_run, force)"] --> LOOP[for each day]
    LOOP --> DONE{"_already_done()?<br/>daily_energy row exists<br/>at current MODEL_VERSION"}
    DONE -- "yes, no --force" --> SKIP["skip day —<br/>fetch/compute/gate never run"]
    DONE -- "no / --force" --> FETCH["fetch getOneDayPowerBySn +<br/>getOneDateEnergyBySn + power_readings"]

    FETCH --> APIERR{ThrottledError / ApiError?}
    APIERR -- Throttled --> THROTTLE["summary.throttled<br/>circuit breaks after 3 consecutive"]
    APIERR -- ApiError --> SUMFAIL[summary.failed]
    APIERR -- none --> EMPTY{"raw empty or blank energy,<br/>or &lt;2 parsed/readings records?"}
    EMPTY -- yes --> SUMEMPTY["summary.empty<br/>(all-zero payload ≠ real zeros)"]
    EMPTY -- no --> COMPUTE["compute_day()"]

    COMPUTE --> WRITERAW["write raw series → metered_power<br/>(always, regardless of gate — kept for recompute)"]
    WRITERAW --> GATE["gate(result)"]

    GATE --> G1{"series_coverage &lt; 0.98<br/>(MIN_SERIES_COVERAGE)?"}
    G1 -- yes --> EXCLUDE
    G1 -- no --> G2{"soc_align_median_pp &gt; 2.0<br/>(MAX_SOC_ALIGN_PP)?"}
    G2 -- yes --> EXCLUDE
    G2 -- no --> G3{"spike_fraction &gt; 0.10<br/>(MAX_SPIKE_FRACTION)?"}
    G3 -- yes --> EXCLUDE
    G3 -- no --> G4{"readings_coverage &lt; MIN_COVERAGE?"}
    G4 -- yes --> EXCLUDE
    G4 -- no --> G5{"series_max_gap_s &gt; 1800<br/>(MAX_SERIES_GAP_S)?"}
    G5 -- yes --> EXCLUDE
    G5 -- no --> WRITE["write daily_energy point<br/>summary.written"]

    EXCLUDE["EXCLUDED (reason) [quality]<br/>summary.gated — nothing written to daily_energy"]

    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef fail fill:#faeaeb,stroke:#c14350,color:#a3323d;
    classDef skip fill:#eceef0,stroke:#6b7280,color:#374151;
    class WRITE ok;
    class EXCLUDE fail;
    class SKIP,SUMEMPTY,SUMFAIL,THROTTLE skip;
```

Gate order matters: series coverage is checked first because a thin series would otherwise
flatteringly understate loss; SoC-clock alignment is checked before the gap check because
clock skew, not data thinness, is the more common real cause of a bad day.

## Spike rejection inside `compute_day()`: `drop_implausible()`

The one true per-sample decision inside metric derivation — everything else in `compute_day()`
(coverage, alignment, kWh integration) is straight-line computation, not branching.

```mermaid
flowchart TD
    S[for each metered 5-min sample] --> C["ceiling = max derived load<br/>within a ±150s window"]
    C --> D{"load &gt; 4.0×ceiling (SPIKE_FACTOR)<br/>AND load−ceiling &gt; 1500W (SPIKE_FLOOR_W)?"}
    D -- yes --> DROP["drop sample<br/>counts toward spike_fraction"]
    D -- no --> KEEP[keep sample]

    classDef fail fill:#faeaeb,stroke:#c14350,color:#a3323d;
    classDef ok fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    class DROP fail;
    class KEEP ok;
```

## Derived metrics (`compute_day`, no branching)

| Metric | Formula |
|---|---|
| `conversion_loss_kwh` | `derived_load_kwh − metered_load_kwh` |
| `charge_minus_discharge_kwh` | `charge_kwh − discharge_kwh` (uncorrected) |
| `delta_soc_percent` | `readings[-1].soc − readings[0].soc` |
| `battery_loss_kwh`* | `charge_kwh − discharge_kwh − delta_soc_kwh` |
| `total_loss_kwh`* | `conversion_loss_kwh + battery_loss_kwh` |

\* only computed when `BATTERY_CAPACITY_KWH` is set.

## Other entry points

| Command | Function | Effect |
|---|---|---|
| (default) | `run_influx()` | main write path above; pushes a heartbeat unless `--dry-run` |
| `--check-alignment` | `run_check_alignment(days)` | sweeps clock offsets −3h..+3h in 5-min steps, reports the offset minimizing SoC disagreement — diagnoses uploadTime timezone bugs. Read-only. |
| `--system-facts` | `run_system_facts()` | logs inverter/battery/capacity from `getEssList`; warns if `BATTERY_CAPACITY_KWH` drifts &gt;1% from reported `cobat`. Read-only. |

## File map

| File | Role |
|---|---|
| `collector/efficiency.py` | `compute_day()`, `gate()`, `process_day()`, `run_influx()`, `drop_implausible()`, `_already_done()`, alignment/system-facts subcommands. |
