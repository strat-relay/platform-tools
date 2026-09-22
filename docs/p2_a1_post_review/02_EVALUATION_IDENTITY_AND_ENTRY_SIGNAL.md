# 02 - Evaluation identity and canonical EntrySignal, verified

## 1. Evaluation identity no longer depends on path, line, chunk position or restart position

Confirmed (`V1`, `V2`, `V4`): `canonical_signal(raw, source_reference=X)` produces the **same** `evaluation.evaluation_hash` for the same logical signal regardless of:

| Case | Result |
|---|---|
| restart (different `source_offset`, same content) | identical hash |
| reread / different chunking (offset 10 vs 900) | identical hash |
| source relocation (`/a/signals.jsonl` vs `/k8s/signals.jsonl`) | identical hash, because `source_reference` is no longer part of `Evaluation.provenance` |
| duplicate delivery | identical hash (deterministic function of content) - the database `ON CONFLICT (evaluation_id) DO NOTHING` absorbs it |
| a genuinely different signal | **different** hash (`V4`); no accidental collision found |

## 2. What now forms the canonical Evaluation identity

`evaluation_hash = sha256(canonical_bytes(Evaluation.to_dict()))`, a full 64-character hex digest (`canonical_hash`, `core/strategies/evaluation/models.py`), over:

```
strategy_id, instrument, decision_time, decision, direction, strategy_version,
parameter_set_id, candidate_id, reason_codes, trace (stage results + fidelity),
runtime_version, evaluator_version, provenance (ALLOWLISTED source-provided keys only)
```

`provenance` now contains only members of `_PROVENANCE_ALLOWLIST` = `{classification, source_config_hash, source_data_age, source_market_data_timestamp, source_process, source_strategy_fingerprint, gap_recovery, orchestrator_freeze_timestamp}` (`V3`). Anything outside that set - including `outcome`, and including the ingest-time `source_reference`/`source_offset`/`source_hash` - never enters the hash.

`entry_signal_hash` (a **second**, independent hash, stored on `strategy.entry_signals` and `strategy.signals`) covers the fuller semantic record: `signal_id, candidate_id, strategy_ref, strategy_id, strategy_version, parameter_set_ref, strategy_instance_id, instrument, direction, decision_time, entry_type, entry geometry, legacy refs, source_event_id, strategy_metadata, source_provenance` (everything in `fields` except the three hash outputs and `signal_emitted_at`, plus `strategy_metadata` and `source_provenance` appended). This is the identity the reconciler and the P4.2 duplicate-hash check (A7 `03` section 6) are meant to use.

## 3. Collision risk

Full SHA-256 (not truncated) for both hashes: negligible collision risk. The real risk category is not hash collision but **hash instability under legitimate reprocessing** (addressed by A1-1/A1-2) and **content divergence being silently accepted** - neither table's `ON CONFLICT DO NOTHING` distinguishes "same content, redelivered" from "different content, same key" at the SQL level; that distinction is exactly what the reconciler and (in P4.2) the `entry_signal_hash` comparison on conflict are for. No collision was found in behavioural testing.

## 4. Canonical EntrySignal vs A7's exact ManagedTrade requirements

| A7 field (`docs/p2_p4_reconciliation/03`) | Present in `strategy.entry_signals`? | Column / source |
|---|---|---|
| `signal_id` | yes | PK, passthrough |
| `candidate_id` | yes | FK-shaped, deterministic |
| Evaluation reference | yes | `evaluation_id` (FK to `strategy.evaluations`) |
| strategy reference | yes | `strategy_id`, `strategy_ref` |
| StrategyVersion identity/reference | **yes, as `strategy_ref`**; `strategy_version_id` intentionally NULL | see `03` |
| ParameterSet identity/reference | yes, nullable with explicit status | `parameter_set_ref`, `parameter_set_status` |
| strategy instance | yes | `strategy_instance_id` |
| instrument | yes | `instrument` (canonical) |
| direction | yes | `direction` |
| `decision_time` | yes, normalised UTC | `decision_time` |
| `signal_emitted_at` | yes | `signal_emitted_at` |
| reference entry semantics | **partial** - raw `entry_type` stored; the derived enum (`CONTEXT_EXECUTABLE_PAPER_FILL` / `LIQUIDITY_REALISTIC_FILL_ELSE_THEORETICAL`) is **not materialised** (`V28`) | see note below |
| entry/stop/target geometry | yes | `entry_price, stop_price, target_price, risk_distance, target_distance, target_r` |
| legacy/source references | yes | `economic_position_id, entry_opportunity_id, setup_id, source_event_id` |
| source provenance | yes, allowlisted | `source_provenance` |
| runtime provenance | yes | `runtime_provenance` (`runtime_version`, `evaluator_version`) |
| Evaluation hash/reference | yes | `evaluation_hash`, `evaluation_id` |
| DecisionTrace reference | yes | `trace_hash` |
| terminal state | yes | `terminal_state = 'ENTRY_SIGNAL_CREATED'` |
| required strategy metadata | yes | `strategy_metadata` (verbatim jsonb) |

**Note on reference entry semantics.** A7's schema (`03` section 2) listed `reference_entry_semantics` as a stored enum; P2-A1 stores only the raw `entry_type`. Since the mapping `entry_type + strategy_id -> reference_entry_semantics` is a static function with no additional inputs, this is **derivable at ManagedTrade-creation time** from data already present, not missing data. It is a schema-completeness gap against the letter of A7 `03`, not a data-sufficiency gap for `CAN_MANAGED_TRADE_BE_CREATED_FROM_CANONICAL_ENTRY_SIGNAL`.

## 5. `CAN_MANAGED_TRADE_BE_CREATED_FROM_CANONICAL_ENTRY_SIGNAL = true`

Every field A7's `03` requires for ManagedTrade creation is present or derivable from `strategy.entry_signals` alone, plus the binding resolver and the environment (both explicitly permitted by A7). None of the forbidden inputs (broker ticket, MT5 account, lot size, execution success, customer entitlement, customer publication) is required or present anywhere in the record or the ingest path (`V33`).
