# 10 - Contract drift check

## 1. File-scope check

Every file changed between `8f49aef` and `3a39fd7` outside `docs/`/`tests/` is confined to `migration/`, `infrastructure/messaging/`, and `postgres/` (`V31`, exhaustive `git diff --name-only`). Frozen strategy files, the Phase 7 observer, the execution consumer, `trade_manager/`, `execution/`, `contracts/`, and `control_api/` are byte-identical to the P2 handoff commit (`V32`).

## 2. Boundary-by-boundary

| Boundary | Introduced? | Evidence |
|---|---|---|
| **P3 broker-state authority** | no | no `broker_state`/`ownership` file touched; no new writer of `runtime/management/*` |
| **P4 Trade Management implementation** | no | no `trade_management/`, `managed_trade`, `trade_manager_decision`, or `trade_observation` table/module anywhere in the diff; migration `011` adds only signal-domain objects |
| **P5 execution** | no | no execution intent, attempt, fence, or bridge-facing code; `contracts/`, `execution/` untouched |
| **Customer publication** | no | no `PublishedSignal`, `ManagementSignal`, or publication-gate code; event payloads carry no publication field |
| **Entitlement** | no | `V33`: no `entitlement`/`subscription_id`/`customer_id` string anywhere in the new code or DDL |
| **Commerce** | no | no Commerce-facing schema or subject |
| **Broker identity** | no | no `broker_ticket`/`lot_size` string anywhere in the new code or DDL (`V33`) |
| **Account identity** | no | no `account_context_id`/`account_id` introduced by P2-A1 (the pre-existing `strategy.signals`/`strategy.candidates` schema, unchanged since before A7, has none either in the signal domain) |

**`CONTRACT_DRIFT_FOUND = false`** on every dimension above.

## 3. Expected-and-confirmed properties

* **Frozen strategy source unchanged**: confirmed (`V32`); this review additionally re-derived the Context decision fingerprint expectation from A5's prior work and found no reason to doubt it, since the files are byte-identical.
* **No Trade Manager policy introduced**: confirmed - `trade_manager/` package untouched; no policy, threshold, or action vocabulary appears in `migration/` or `postgres/migrations/011_*.sql`.
* **No broker writes**: confirmed - no bridge client import, no `BROKER_WRITES` counter touched, `BROKER_WRITES=0` throughout (this review performed none either).
* **No bridge changes required**: confirmed - P2-A1 does not depend on, reference, or require any change to `mt5-native-bridge`; the OD-06 fencing work (A5 ADR-0003) is untouched and remains a P5-only concern.

## 4. One item worth flagging as "adjacent to but not crossing" a boundary

The `docs/migration/p2_1_live_shadow_evidence/` preflight (`09` section 1) read **live K8s state** (pod names, container status, an orchestrator manifest field, a bridge health endpoint) through kubectl/HTTP, entirely read-only, with an explicit written record of zero mutation. This is not a contract-drift concern (no authority, schema, or runtime state changed) but is the first point in this lineage where a task actually touched a live cluster rather than only source and a local database; it is recorded here for completeness because the mission asked this review to "check for contract drift" broadly and because future reviewers should know it happened, correctly, under read-only constraints.
