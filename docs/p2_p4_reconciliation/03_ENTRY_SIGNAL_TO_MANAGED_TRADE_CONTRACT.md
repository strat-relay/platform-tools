# 03 - The exact EntrySignal -> ManagedTrade contract

Supersedes A6 `06_MANAGED_TRADE.md` sections 2-3 where they differ (A7 > A6). A6's model (ManagedTrade in Trading Core, created on `signal.entry.created`, 1:1 with the EntrySignal, independent of publication/execution) is **confirmed**. What changes is what the *actual* P2 event and record provide.

## 1. Does the actual P2 event contain everything required? **No.**

`signal.entry.created.v1` (`R5`) carries `signal_id, candidate_id, evaluation_id, evaluation_hash, trace_hash, source_reference`. The persisted signal record (`R4`) is the Evaluation dict. Missing for ManagedTrade creation:

| Needed by ManagedTrade | In P2 event? | In P2 DB? | Source in the legacy `StrategySignal` |
|---|---|---|---|
| instrument, direction, decision_time | no | yes, via `strategy.evaluations` | `canonical_symbol`, `direction`, `decision_time` |
| **entry, stop, target, risk_distance** | **no** | **no** | `entry_price`, `stop_price`, `target_price`, `risk_distance` |
| reference entry semantics / entry type | no | partly (`entry_type` in stage metadata) | `entry_type` (`MARKET_PAPER_OBSERVATION` / `MARKET`) |
| strategy id | no | yes | `strategy_id` |
| strategy version identity | no | ambiguous label `V1` (`R7`) | `strategy_version` |
| ParameterSet identity | no | always NULL (`R9`) | `strategy_metadata` (Liquidity `entry_fraction`) |
| strategy instance | no | **no** | `strategy_instance_id` |
| legacy refs (`economic_position_id`, `entry_opportunity_id`, `setup_id`, `source_event_id`) | no | **no** (`source_event_id` only inside a derived candidate id) | same names |
| discovery time (`signal_emitted_at`) | as `occurred_at` only | no | `signal_emitted_at`, `created_at` |
| provenance classification (`PROSPECTIVE_ORCHESTRATOR_SIGNAL`, `gap_recovery`) | no | yes, mutated/hash-unstable (`R12`) | `provenance` |

Therefore `ENTRY_SIGNAL_MANAGED_TRADE_INPUT_SUFFICIENT = false`. The gap is closed by the P2 amendment in `12` (an **EntrySignal canonical record v1** plus a payload extension) - **not** by making ManagedTrade creation read legacy files, and not by inventing a SignalStream.

## 2. EntrySignal canonical record v1 (interface owned by the Signals/strategy domain; delivered by P2 amendment)

`strategy.entry_signals` (one row per `signal_id`, immutable), plus `entry_signal_hash = sha256(canonical_bytes(record without the hash))`:

```
signal_id                    PK, FK strategy.signals        (legacy stable id, verbatim)
strategy_id, strategy_version (label), strategy_instance_id
strategy_ref                 "<strategy_id>@<strategy_version>"   # NOT the shared 'V1' key; see 04 section 3
parameter_set_ref            nullable; see section 5
instrument                   canonical (XAUUSD)          broker_symbol_hint  (informational, not an identity)
direction                    LONG | SHORT
decision_time                UTC timestamptz  (reference fill time; normalised)
reference_entry_price, initial_stop, initial_target, risk_distance, target_r
reference_entry_semantics    enum: CONTEXT_EXECUTABLE_PAPER_FILL | LIQUIDITY_REALISTIC_FILL_ELSE_THEORETICAL   (from entry_type + strategy family, static legacy mapping)
entry_type                   MARKET_PAPER_OBSERVATION | MARKET | ... (verbatim)
timeframe, lower_timeframe, higher_timeframes, entry_mechanism, strategy_metadata (jsonb, verbatim)
legacy_refs                  { economic_position_id, entry_opportunity_id, setup_id, source_event_id, market_event_id }
signal_emitted_at, source_created_at
provenance_class             PROSPECTIVE_ORCHESTRATOR_SIGNAL | GAP_RECOVERY | PRE_ORCHESTRATOR_REFERENCE | ...   (from legacy provenance, verbatim)
evaluation_id, evaluation_hash, trace_hash      # references
source_ref                   { path, offset }  # NOT hashed
entry_signal_hash
```

`signal.entry.created.v1` payload adds `entry_signal_hash`, `strategy_id`, `instrument`, `direction`, `decision_time` (small facts, useful for filtering and for detecting a stale/mismatched read) but **the record is authoritative**; the consumer reads it by key and verifies the hash.

## 3. ManagedTrade minimum creation input

Everything below is derived **only** from the EntrySignal record, the binding resolver, and the trading environment - **never** from publication, entitlement, Personal Execution, an account, a ticket, a lot size, a broker position, or execution success.

| Field | Source | Notes |
|---|---|---|
| `managed_trade_id` | `stable_id("MT", {"signal_id": signal_id})` | section 4 |
| `entry_signal_id` | `signal_id` | UNIQUE for the BOUND track |
| `entry_signal_hash` | record hash | verified at creation; stored |
| `signal_stream_id` | resolver output, **nullable seam** (section 5) | not invented by P4 |
| `strategy_id`, `strategy_version`, `strategy_ref` | record | |
| `strategy_version_id` | **NULL until a real StrategyVersion registry exists**; `strategy_ref` is the binding key meanwhile | do not reuse the shared `'V1'` row |
| `parameter_set_id` / `parameter_set_ref` | record / resolver; nullable, with explicit status `LEGACY_IMPLICIT_IN_STRATEGY_ID` | section 5 |
| `instrument`, `direction` | record | canonical instrument |
| `decision_time` | record (UTC) | reference fill time; the trade's `opened_at` for the reference model |
| `reference_entry_price`, `initial_stop`, `initial_target`, `risk_distance`, `reference_entry_semantics` | record | frozen copies; never recomputed |
| `trade_manager_version_id`, `tm_binding_id`, `tm_bound_at` | resolver + `04` | immutable |
| `market_feed_id` | resolver; nullable (`FEED_UNBOUND`) until P4.3 records the reference feed | evidence-relevant, `05` of A6 |
| `evidence_mode` | environment + `provenance_class` | `FORWARD` / `REPLAY` / `BACKTEST` (`08`) |
| `eligibility` | computed at creation | `PROSPECTIVE_ELIGIBLE`, `FORWARD_INELIGIBLE(reason)`: `LATE_CREATION`, `GAP_RECOVERY`, `PRE_ORCHESTRATOR_REFERENCE`, `TM_VERSION_NOT_FROZEN_AT_OPEN` |
| `observation_start_lag_seconds` | `created_at - decision_time` (recorded, evaluated by policy `L`) | the Context/Liquidity adapters emit **after** the reference fill, so a first observation cannot precede creation: the interval `[decision_time, first observation]` is unobserved by design |
| `legacy_refs` | record | Context `economic_position_id` etc.; **reference only**, never an identity |
| `record_mode` | `SHADOW` (P4.2) / `PRIMARY` (P4.5) | |
| `state`, `version` | `OPEN` (both adapters emit only post-fill) / `PENDING_ENTRY` / `CLOSED` / `CANCELLED` | `PENDING_ENTRY` is schema-only in P4 |
| `created_at` | DB time | |
| `source_signal_emitted_at` | record | discovery time, for lag metrics |

**Explicitly absent**: `published_signal_id` (nullable *reference* only, added later by the Signals domain, never an input), any customer/entitlement/subscription field, account or account mode, ticket, lot/volume, broker position, execution status, `execution_intent_id`. A property test asserts the DDL contains no such column (`10`).

## 4. Is `stable_id("MT", {signal_id})` sufficient and safe? **Yes, with stated conditions.**

| Requirement | Assessment |
|---|---|
| unique per EntrySignal | `signal_id` is the PK of `strategy.signals`; legacy identity includes strategy id, version, instance and source event id (`01`) - re-entries have distinct economic ids and therefore distinct signals |
| deterministic across restart/duplicate/environment | yes (`signal_id` passthrough is stable, `R11`; `stable_id` is sha256-truncated) |
| independent of everything forbidden | yes: no account, ticket, publication, execution |
| collision risk | 96-bit prefix; negligible; the DB UNIQUE constraint on `entry_signal_id` is the real guard |
| **conditions** | (1) `signal_id` is treated as **opaque and immutable** - a re-derivation under a new strategy version is a *different* signal and a different ManagedTrade by design; (2) an `entry_signal_hash` mismatch on a replayed event (same `signal_id`, different content) is a **reconciliation finding, never a silent overwrite** (`ON CONFLICT DO NOTHING` must not hide it); (3) the BOUND track is the only one whose id is `MT_...`; challenger tracks are `ManagedTradeEvaluationTrack` rows keyed `(managed_trade_id, tm_version_id)` (A6 `06`, resolved in `04`) |
| what it must not encode | strategy version, TM version, account: those are attributes, so a TM upgrade never changes a trade's identity |

## 5. SignalStream and ParameterSet: the required seam (nothing invented)

P2 has **no SignalStream** and no StreamBinding. A2 defines `SignalStream` as a Trading Core/Commerce-adjacent concept (strategy + instrument + parameters, with bindings that name the TradeManagerVersion). The seam:

```
StreamBindingResolver.resolve(entry_signal_record, at = decision_time) ->
    { signal_stream_id | None, strategy_ref, parameter_set_ref | None, trade_manager_version_id,
      market_feed_id | None, binding_id, binding_hash, resolution = STREAM | LEGACY_STATIC | DEFAULT_TM_NONE }
```

* **P4.2 ships only `LEGACY_STATIC` and `DEFAULT_TM_NONE`**: a versioned static table `trade_management.legacy_stream_binding(binding_id, strategy_id, strategy_instance_id?, instrument?, tm_version_id, market_feed_id?, valid_from, valid_to?, binding_hash)` seeded by migration/config, plus a fallback to the frozen `TM-NONE-1` version when no row matches. `signal_stream_id` stays NULL.
* The `STREAM` resolver is a later Trading Core/Commerce-boundary deliverable; when it exists it plugs into the same interface and **only affects trades opened afterwards**.
* **ParameterSet**: legacy `parameter_set_ref` is NULL with status `LEGACY_IMPLICIT_IN_STRATEGY_ID` (each Liquidity instance is its own `strategy_id`; A3's "four ParameterSets of one definition" is a later mapping). The EntrySignal record keeps `strategy_metadata` verbatim so that mapping is lossless later.

## 6. Creation transaction and duplicate semantics

One transaction: inbox claim (`trade-mgmt-open`, event id) -> read `strategy.entry_signals` by `signal_id`, verify `entry_signal_hash` -> resolve binding (immutable at this instant) -> `INSERT managed_trade ... ON CONFLICT (entry_signal_id) DO NOTHING` -> `INSERT managed_trade_state_history` -> outbox `trade.opened.v1` -> mark inbox processed -> commit -> ack. Redelivery: inbox hit. Missing EntrySignal record: retry with backoff, then quarantine `ENTRY_SIGNAL_RECORD_MISSING` (never create from the event payload alone). Ineligible signals (`GAP_RECOVERY`, `PRE_ORCHESTRATOR_REFERENCE`, late creation) still get a ManagedTrade with `eligibility = FORWARD_INELIGIBLE(reason)` so nothing is silently dropped and the exclusion is auditable.

## 7. Independence statements (each is a test in `10`)

ManagedTrade creation succeeds identically with Personal Execution absent, no broker/bridge credentials present, no entitlement or subscription tables, no `PublishedSignal`, and no Phase 7 process. It is independent of publication (`PUBLISHED`/`WITHHELD` never read), of customer entitlement, and of execution success.
