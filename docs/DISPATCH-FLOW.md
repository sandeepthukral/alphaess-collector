# Dispatch decision flow

How a planner forecast becomes a live inverter command. Two clocks run this system: a batch
planning run (external LP solver, hours ahead) and a live loop that polls the inverter once a
minute and only ever acts on the current slot. See `DESIGN-dispatch.md` for narrative context;
this doc is the visual reference for the control flow itself.

## Pipeline overview

```mermaid
flowchart LR
    P["battery-planning<br/>LP planner<br/>Wh forecast"] -->|batch, hrs ahead| T
    T["translator.py<br/>classify() / to_slots()<br/>→ slots.json"] -->|writes| S
    S["scheduler.py<br/>tick()<br/>read live SoC/grid/batt<br/>→ surplus_w"] -->|every 60s| D
    D["slots.py<br/>decide() / clamp()"] -->|verified| I["inverter · apply/release<br/>readback verify, 1 retry<br/>+ cell voltage/temp, health gates<br/>→ InfluxDB + Kuma"]
```

`dispatch/plan.py` → `dispatch/translator.py` → `dispatch/slots.json` → `dispatch/scheduler.py`
→ `dispatch/slots.py` → Modbus register write.

## Live decision: `decide()`

Runs on every 60s tick (`dispatch/slots.py:231-323`, driven by `dispatch/scheduler.py:310-579`).
Re-validates the plan's chosen action against the *live* state of charge before anything reaches
the inverter — a planned charge/discharge is downgraded to hold once the target is within a
0.4% deadband of live SoC.

```mermaid
flowchart TD
    A["tick() · every 60s"] --> B[reload slots.json if changed]
    B --> C["read live SoC / grid_w / battery_w<br/>→ surplus_w = −(grid_w + battery_w)"]
    C --> D{doc is None?}
    D -- yes --> IDLE1[IDLE · no plan]
    D -- no --> E{"plan fresh?<br/>age &lt; 2h · before horizon"}
    E -- no --> IDLE2[IDLE · stale plan]
    E -- yes --> F{"slot found?<br/>find_slot(now)"}
    F -- no --> IDLE3[IDLE · no slot / gap]
    F -- yes --> G{slot.action}

    G -- self --> SELF[self-consume] --> REL1((RELEASE))
    G -- hold --> H{surplus_w &gt; 200W?}
    H -- yes --> REL2(("RELEASE<br/>PV-spill override"))
    H -- no --> HOLD1["HOLD · 0W"]
    G -- charge --> I{"target ≤ live_soc + 0.4%?"}
    I -- yes --> J["target reached:<br/>release if surplus, else hold"]
    I -- no --> CHG["Command +power_w<br/>SOC_TARGET · 300s"]
    G -- discharge --> K{"target ≥ live_soc − 0.4%?"}
    K -- yes --> HOLD2["HOLD · target reached"]
    K -- no --> DIS["Command −power_w<br/>SOC_TARGET · 300s"]

    REL1 --> CLAMP
    REL2 --> CLAMP
    HOLD1 --> CLAMP
    J --> CLAMP
    CHG --> CLAMP
    HOLD2 --> CLAMP
    DIS --> CLAMP

    CLAMP["clamp(): cap to min(inverter limit, 5000W)<br/>0W ceiling in that direction → hold instead"] --> M{"hijacked?<br/>another writer"}
    M -- yes --> SKIP[SKIP · log only]
    M -- no --> N["apply via Modbus (Command) / release() /<br/>idle → release once then go silent"]
    N --> O[verify via register readback · 1 retry]
    O --> TEMP["read min/max cell voltage & temp<br/>published only, never decides"]
    TEMP --> HEALTH["hourly/weekly health gates<br/>fault block + firmware/config<br/>published only, never decides"]
    HEALTH --> PUB["publish dispatch_state → InfluxDB<br/>heartbeat → Kuma"]
    PUB --> A

    classDef release fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef hold fill:#eceef0,stroke:#6b7280,color:#374151;
    classDef charge fill:#faefdd,stroke:#b8790f,color:#8a5a0a;
    classDef discharge fill:#eceafc,stroke:#5b52c9,color:#4038a0;
    classDef idle fill:#faeaeb,stroke:#c14350,color:#a3323d;
    class REL1,REL2 release;
    class HOLD1,HOLD2,J hold;
    class CHG charge;
    class DIS discharge;
    class IDLE1,IDLE2,IDLE3,SKIP idle;
```

`dispatch/slots.py:231-323` (decide) and `:326-367` (clamp). The charge/discharge "target
reached" outcomes release if `surplus_w > 0`, else hold.

## Planning-time classification: `classify()`

Upstream of the above — runs once per planner batch, not per tick. `translator.py` turns each
LP interval's Wh forecast into the `slot.action` that `decide()` later re-checks live.

```mermaid
flowchart TD
    S[per plan interval] --> D1{discharge_wh &gt; floor?}
    D1 -- yes --> D2{export_wh &gt; floor?}
    D2 -- yes --> DIS[discharge]
    D2 -- no --> SLF1[self]
    D1 -- no --> C1{charge_wh &gt; floor?}
    C1 -- yes --> C2{import_wh &gt; floor?}
    C2 -- yes --> CHG[charge]
    C2 -- no --> SLF2[self]
    C1 -- no --> H1{"at capacity +<br/>near-zero import?<br/>_can_harvest()"}
    H1 -- yes --> SLF3[self]
    H1 -- no --> HLD[hold]

    classDef release fill:#e5f4ec,stroke:#1f8f56,color:#166a3f;
    classDef hold fill:#eceef0,stroke:#6b7280,color:#374151;
    classDef charge fill:#faefdd,stroke:#b8790f,color:#8a5a0a;
    classDef discharge fill:#eceafc,stroke:#5b52c9,color:#4038a0;
    class SLF1,SLF2,SLF3 release;
    class HLD hold;
    class CHG charge;
    class DIS discharge;
```

`dispatch/translator.py:156-189` (classify) and `:90-153` (`_can_harvest` — the at-capacity
PV-surplus override). `to_slots()` (`:192-287`) then converts the action to `power_w`/
`target_soc` and downgrades charge/discharge to hold if the target doesn't actually move away
from the interval's own start-of-interval SoC.

On a `discharge` action, `power_w` normally derives from `discharge_wh × 60/minutes`, but
`to_slots()` (`:217-226`) uses `iv.discharge_power_w` instead whenever the plan supplies one.
`Marstek-planning.py` sets that field only on intervals it planned at the discharge ceiling
(`maxDischargeSpeed`), to a setpoint (`maxRequestedDischargeSpeed`, currently 5000 W) above
what `discharge_wh` alone implies — the inverter delivers roughly 300 W less than a sustained
discharge setpoint asks for (investigated 2026-08-24), so reaching the true achievable ceiling
requires commanding above it. `discharge_wh`/`soc_wh` themselves are never touched — only the
wire setpoint for that one interval changes. Not done for `charge`: measured charge sessions
already meet or exceed their setpoint, so there is nothing to compensate for (yet).

## File map

| File | Role |
|---|---|
| `dispatch/plan.py` | Parses the LP planner's raw output. |
| `dispatch/translator.py` | `classify()` + `to_slots()` + `build_document()` → writes `slots.json`. |
| `dispatch/slots.py` | `decide()` and `clamp()` — the pure decision function this doc mostly diagrams. |
| `dispatch/scheduler.py` | `tick()` — the 60s loop: read live state, call decide/clamp, actuate, verify, publish. |
| `dispatch/registers.py` | `Command`, `DispatchMode`, Modbus register encode/decode. |
| `dispatch/slot_publisher.py`, `state.py` | Publishing helpers for slots.json state and `dispatch_state`. |
