# 04 - TradeManagerVersion V1 contract (required before P4.2)

Formalises A6 `07_TRADE_MANAGER_VERSIONING.md` (A7 > A6). No real management strategy is created: the first canonical version is **`TM-NONE-1`**. Nothing here changes any legacy Trade Manager code or policy.

## 1. Version identity

```
tm_version_id = "TMV_" + sha256(canonical_bytes(identity_manifest))[:24]      # canonical_bytes: core/strategies/evaluation (the V1.1 canonicaliser)

identity_manifest  (schema "tm-version-manifest.v1"; EVERY member is hashed)
  manifest_schema                "tm-version-manifest.v1"
  evaluator_id                   "tm-none.v1"                      # names the evaluation semantics; not a label
  decision_schema_version        "trade-manager-decision.v1"
  action_vocabulary_version      "tm-actions.v1"                   # HOLD MOVE_STOP MOVE_TO_BREAKEVEN TRAIL_STOP PARTIAL_PROFIT EXIT
  reason_code_registry_version   "tm-reasons.v1"
  policy_bundle                  [ {policy_id, version, strategy_id, enabled, breakeven, profit_protection, trailing, partial_reduction, early_exit, adding, reentry, context} ... ]   # canonicalised full content; [] for TM-NONE
  resolution_table               [ {strategy_id, instrument|"*", policy_id|null} ... ]   # AFTER inheritance/merge; what "resolve(strategy, instrument)" returns; TM-NONE: everything -> null
  observation_spec
    timeframes_consumed          []  for TM-NONE (no bar-derived signal is consumed)
    ema_period, swing_lookback_left, swing_lookback_right, ema_tolerance, structure_tolerance    # null when unused
    max_market_age_ms            integer (staleness rule), late_event_rule "IGNORE_NEVER_EVALUATE"
    quote_required               true
  price_semantics
    version                      "ps.v1"        # close side = bid for LONG / ask for SHORT; mark = mid
    reference                    semantics.py content hash at freeze
  arithmetic
    r_multiple_definition        "(price-entry)/|entry-initial_stop| signed by direction"
    rounding / precision         explicit; tie-break order of rule evaluation
  code_manifest                  { "<path>": sha256(file bytes) }   # every module whose behaviour defines the decision, as executed
  scope                          { strategies: ["*"] | [...], instruments: ["*"] | [...] }
```

**Excluded from the hash** (they must not make identical behaviour look different): label, description, `frozen_at`, `derived_from`, author, environment, host, `runtime_instance_id`, transport, promotion state.

Canonicalisation rules: keys sorted, numbers in the V1.1 canonical form, no floats from computed expressions (all thresholds are literals in the manifest), `null` for absent members (never omitted), lists ordered where order is semantic and sorted where it is a set.

## 2. TM-NONE-1 (the first canonical version)

| Member | Value |
|---|---|
| `evaluator_id` | `tm-none.v1` |
| policy bundle / resolution | empty / every `(strategy, instrument)` -> no policy |
| behaviour | for every observation returns `action = HOLD`, `parameters = {}`, `reason_codes = [NO_MANAGEMENT_POLICY]`, or - by the rules of A6 `08` section 4 - `HOLD` with `STALE_MARKET_DATA`, `DATA_UNAVAILABLE`, `INVALID_TRADE_STATE`, `TRADE_CLOSED`. It **decides nothing** about management |
| derived state | it *measures* (current R, MFE/MAE accumulated on the ManagedTrade, elapsed time) and records it as decision evidence; measurement is not policy |
| status | `FROZEN` at registration; publication eligibility `SHADOW_ONLY` (it never produces an actionable decision, so nothing can be published) |
| purpose | proves ManagedTrade, observation, decision persistence, publication gate (all `WITHHELD/not actionable`) and evidence plumbing with audited `HOLD`s; **no validated management policy exists** (Phase 7's own report flags N < 20) |

`TM-LEGACY-0` (the code and default registry at `17c09b2`) is registered **for provenance only** with `status = RETIRED`-from-birth; it may never be bound.

## 3. `strategy_version_id` must not be the shared `'V1'`

P2 registers `platform.strategy_versions('V1')` once, for Context *and* Liquidity, with a per-first-signal `source_hash` (`R7`, `R8`). The TM version binding and the ManagedTrade therefore key strategy identity by **`strategy_ref = "<strategy_id>@<strategy_version>"`** (recorded in the EntrySignal record) and leave `strategy_version_id` NULL until a real, unique StrategyVersion registry exists (A3 lifecycle). This is an A7 adjustment to A6, not a P2 change.

## 4. Lifecycle

A version's **manifest is immutable and its id is its hash**, so there is no mutable "draft" inside the runtime database (drafts live in the authoring plane, A3).

| State | Meaning | Mutation |
|---|---|---|
| `FROZEN` | registered; may be bound to trades opened afterwards | none (immutability trigger; no UPDATE privilege) |
| `RETIRED` | no new bindings | one-way, append-only record |

Operational facts that change over time live in **append-only records** so they never touch the hashed manifest: `tm_version_promotion(tm_version_id, from, to, decided_by, decided_at, evidence_ref)` with values `SHADOW_ONLY` -> `PUBLISHABLE`; `frozen_at` starts the FORWARD clock (`08`). Deriving a version (any change, even one parameter) creates a **new** id with `derived_from` lineage and a **new** FORWARD clock; there is no in-place edit.

## 5. When a ManagedTrade selects its version, and proof that it never silently rebinds

**Selection instant.** In the *creation transaction* of the ManagedTrade (`03` section 6): the binding resolver is consulted **exactly once**, as of `decision_time`, and its result (`tm_version_id`, `tm_binding_id`, `binding_hash`, `tm_bound_at`) is written into the row. Nothing consults the resolver or the registry afterwards.

**Prevention, by construction (each is a required test):**

| # | Mechanism | Defeats |
|---|---|---|
| 1 | binding columns (`tm_version_id`, `tm_binding_id`, `binding_hash`, `tm_bound_at`, `strategy_ref`, `entry_signal_hash`) are **immutable**: a `BEFORE UPDATE` trigger raises on any change, and the application role has no `UPDATE` on them | edits by code or operator |
| 2 | `trade_manager_version` rows are immutable (trigger), `manifest_hash` UNIQUE | editing a version in place |
| 3 | `trade_manager_decision` has a composite FK `(managed_trade_id, tm_version_id) -> managed_trade(managed_trade_id, tm_version_id)` | a decision under any version other than the bound one |
| 4 | the evaluator loads the manifest **by `managed_trade.tm_version_id`** and never by resolving `(strategy, instrument)`; it asserts `observation.tm_version_id == row.tm_version_id` | the legacy behaviour where `PolicyRegistry.resolve` runs on every observation |
| 5 | at start the evaluator recomputes the **code manifest** and refuses versions it cannot reproduce (fail closed, A6 `18` row 13) | a silent code edit |
| 6 | the binding table is append-only with `valid_from`; changing a binding creates a new row that only affects trades opened at or after it (verified by a test that changes the resolver and re-reads an existing trade) | mid-trade registry change |
| 7 | a "rebind" does not exist; evaluating a trade under another version is an explicit `ManagedTradeEvaluationTrack(role=CHALLENGER)` row that never touches the BOUND track | ad-hoc reassignment |

This directly replaces the current behaviour that A6 `06` documented: `PolicyRegistry.resolve` is invoked inside `Phase2TradeManager.evaluate` for every observation, and `management_policy_version` is a bare label (A6 `M10`).

## 6. What P4.1/P4.2 must and must not do about versions

* P4.1 defines the manifest schema, canonicalisation, hashing tool, DDL, immutability triggers, the `TM-NONE-1` manifest and its **golden hash vector**, and registers `TM-NONE-1` and `TM-LEGACY-0`.
* P4.2 binds every new ManagedTrade to `TM-NONE-1` (via `LEGACY_STATIC` or `DEFAULT_TM_NONE`) and records that.
* Neither creates a real policy, edits legacy `trade_manager/`, or reads `PolicyRegistry` at runtime.
