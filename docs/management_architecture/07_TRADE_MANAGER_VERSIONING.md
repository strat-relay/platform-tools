# 07 - TradeManagerVersion: minimum identity model (design only; nothing implemented)

## 1. Requirement

A management decision must be **reproducible against the frozen TradeManager that emitted it**: given the same observation and the same persisted trade state, the same `tm_version_id` yields the same decision, byte for byte. A change to *anything that can change a decision* must produce a **different** version id and start its own FORWARD evidence clock (A2 `06` series key; the task's Part P).

## 2. What identifies a version today (and the gap)

| Today | Evidence |
|---|---|
| `ManagementPolicyConfig.identity` = `"{policy_id}:{version}"` (labels) | `policies.py` |
| `management_policy_version` in the decision = `policy.version` (`"1"`) | `phase2.py` |
| no hash of policy code, parameters, resolution table, observation parameters or price semantics | `M10` |
| resolution by `(strategy_id, symbol)` re-evaluated on every observation | `phase2.evaluate` -> `registry.resolution(position)` |
| research parameter sets (`ExperimentSpec.parameters`) live in a different object | `experiments.py` |
| decision carries `mode = ADVISORY_SHADOW` / `advisory_only` regardless of version | `engine._record` |
| observation parameters (EMA period 200, swing lookback 2/2, tolerances) are code constants | `observation.py` |

A `version: "1"` label survives any edit to `engine.py`, `trailing.py`, `semantics.py`, `observation.py` or the default parameters - the exact drift that would make a "frozen" Trade Manager unreproducible.

## 3. Proposed identity

```
TradeManagerVersion
  tm_version_id      = "TMV_" + sha256(canonical_bytes(identity_manifest))[:24]     # canonical bytes as in core/strategies/evaluation
  label              free text (not hashed), e.g. "TM-NONE-1", "CONTEXT-BE075-1"
  status             DRAFT -> SHADOW -> ACTIVE -> FROZEN -> RETIRED     (A3 version lifecycle vocabulary; never mutated after FROZEN)

identity_manifest  (every member is hashed; any change = new version)
  policy:
    policy_bundle:            [ {policy_id, version, strategy_id, enabled, breakeven, profit_protection, trailing, partial_reduction, early_exit, adding, reentry, context} ... ]   # full ManagementPolicyConfig content, canonicalised
    resolution_table:         { strategy_default and instrument_override entries, AFTER inheritance/merge }                                                              # what PolicyRegistry.resolve() would return, per (strategy, instrument)
  evaluator:
    code_manifest:            { "trade_manager/engine.py": sha256, "trailing.py": sha256, "policies.py": sha256, "phase2.py": sha256, "semantics.py": sha256, "observation.py": sha256 }
    action_vocabulary_version, reason_code_registry_version, decision_schema_version
  observation_spec:
    timeframes_consumed:      ["M5"]  (M1 only if a policy consumes it)
    ema_period, swing_lookback_left/right, ema_tolerance, structure_tolerance
    max_market_age_ms, staleness rule, late-event rule
    price_semantics_version   (semantics.py hash + rule text)
  arithmetic:
    numeric precision / rounding rules, tie-breaking, ordering of rule evaluation
  scope:
    applicable strategies/instruments (StreamBinding filters)
```

Excluded from the hash (would make identical behaviour look different): label, description, timestamps, owner, environment, deployment host, `runtime_instance_id`, and the *event transport*.

### 3.1 Contributors, by the task's list

| Contributor | In the manifest | Note |
|---|---|---|
| policy source/hash | `evaluator.code_manifest` | hash of the modules whose behaviour defines the decision; the runtime asserts them at start (like A5's host pins) |
| parameters | `policy.policy_bundle` | including `parameter_status` (`UNVALIDATED`) |
| strategy-specific configuration | `policy.resolution_table` (per strategy) | today `context_v1_experiment()` |
| instrument-specific configuration | `policy.resolution_table` (per instrument override, post-merge) | override inheritance is code (`register_instrument_override`); the merged result is what is hashed |
| price semantics | `observation_spec.price_semantics_version` | close side = bid for LONG / ask for SHORT; mark = mid |
| trailing policy | inside `policy_bundle.trailing` + `trailing.py` in `code_manifest` | method (R_BASED, STRUCTURE, EMA_STRUCTURE, ATR_STRUCTURE) and parameters |
| breakeven policy | `policy_bundle.breakeven` | activation R, protected R |
| partial policy | `policy_bundle.partial_reduction` | fraction, conditions |
| observation parameters | `observation_spec` | not a "policy" today but changes decisions |

## 4. Binding, freezing, derivation

* **Binding**: `StreamBinding` (A2) names a `trade_manager_version_id`; a ManagedTrade copies it at open (`06`). The registry is **not consulted per observation** any more; the bound version's frozen `resolution_table` is.
* **Freezing**: a version is `FROZEN` when its manifest is hashed and recorded with `frozen_at`; only `FROZEN` (or `SHADOW`, unpublished) versions may be bound. The evidence clock (`15`) starts at `frozen_at`.
* **Derivation**: any change (even a parameter) creates a new version; `derived_from` links it to its parent for lineage; there is **no in-place edit**.
* **Runtime check**: at start the evaluator verifies the code manifest against the running files and refuses to evaluate for a version it cannot reproduce (fail closed; `18`).

## 5. The first versions

Because the only registered policy has every action disabled and always returns `HOLD` (`M9`, `M25`), the honest first frozen version is a **`TM-NONE` version** (all management disabled): it proves the whole pipeline - ManagedTrade, observation, decision persistence, publication gate, evidence - while emitting only audited `HOLD`s. Real policies (breakeven/trailing/partial) are separate, later versions gated by the validation evidence Phase 7 is still collecting (its own report flags N < 20 as too small for selection). Freezing `TM-NONE` does **not** decide any management policy.

## 6. Legacy identity to record

`TM-LEGACY-0` = the code and default registry at `17c09b2` (code manifest hashes of the six modules + the `context_v1_experiment` bundle). It is recorded for provenance and for the golden-replay baseline; it is not bound to any trade.
