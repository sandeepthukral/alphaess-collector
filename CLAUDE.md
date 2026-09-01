# Repo instructions

## Flow docs must stay in sync with decision logic

`docs/*-FLOW.md` are Mermaid flowcharts of this repo's decision-logic modules — branches,
thresholds, gates, severities. Any change to the branching/thresholds/ordering/states in the
files below must update the matching doc in the same change, not as a follow-up:

| Code | Doc |
|---|---|
| `dispatch/translator.py` (`classify()`/`to_slots()`), `dispatch/slots.py` (`decide()`/`clamp()`), `dispatch/scheduler.py` tick loop | [docs/DISPATCH-FLOW.md](docs/DISPATCH-FLOW.md) |
| `dispatch/reliability.py` (`analyse()` and its checks/severities), `scripts/review-dry-run.py`, `scripts/is-it-deciding.py` | [docs/RELIABILITY-FLOW.md](docs/RELIABILITY-FLOW.md) |
| `collector/efficiency.py` (`compute_day()`, `gate()`, `process_day()`, `drop_implausible()`) | [docs/EFFICIENCY-FLOW.md](docs/EFFICIENCY-FLOW.md) |
| `collector/prices.py`, `collector/pricing.py` (`compute_day()`, `gate()`, `audit_day()`) | [docs/PRICING-FLOW.md](docs/PRICING-FLOW.md) |
| `collector/mijnbatterij.py` (`collect()`, `build_payload()`, the submit/backoff loop) | [docs/MIJNBATTERIJ-FLOW.md](docs/MIJNBATTERIJ-FLOW.md) |

When adding a new decision-logic module worth diagramming, add both the doc and a row here.
