# 03 - Strategy / version / parameter identity, and provenance

## 1. Context V1 vs Liquidity V1 no longer collide

Both strategies still label `strategy_version = "V1"` (unchanged legacy behaviour; not something P2-A1 should or does touch). What changed is that P2-A1 **stopped using that label as a shared foreign key**:

* `strategy_ref = f"{strategy_id}@{strategy_version}"` disambiguates every strategy (`CONTEXT_STRUCTURE_RETRACE_V1@V1` vs `LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1@V1`, confirmed distinct in `V5`).
* `persist_evaluation(conn, signal.evaluation, strategy_version_id=None)` and the `strategy.candidates` insert both write `strategy_version_id = NULL` (`V5`, `V6`); no code path inserts into `platform.strategy_versions` any more.

So the previous defect (A5/A7 findings `R7`/`R8`: one shared `platform.strategy_versions('V1')` row, its `source_hash` overwritten by whichever strategy ingested first) is **fully resolved by removal**, not by making the shared row correct. `platform.strategy_versions` is simply not written by the signal path; a real, unique StrategyVersion registry remains future work (A7 `OD-A7-6`), and `strategy_ref` is the interim binding key, exactly as A7 `04` specified.

## 2. Frozen source identity is preserved

No frozen strategy file, the Phase 7 observer, or the execution consumer changed (`V32`). P2-A1 does not read or assert against the runners' own fingerprints (`70dba71d...` for Context, `source_sha256` for Liquidity); it only forwards whatever `source_strategy_fingerprint`/`source_config_hash` the legacy adapter already placed in `provenance`, and only if those keys are in the allowlist (they are: `source_config_hash`, `source_strategy_fingerprint`). Nothing about frozen identity enforcement was touched, weakened, or duplicated.

## 3. ParameterSet identity survives ingestion for Liquidity variants

Each Liquidity instance is its own `strategy_id` (e.g. `LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1`), so `strategy_ref` already distinguishes instances. Within one instance, the parameter (`entry_fraction`) travels in `strategy_metadata`, preserved verbatim and folded into `entry_signal_hash`: two otherwise-identical raw records with different `entry_fraction` produce different `entry_signal_hash` values (`V7`). Nothing is dropped.

## 4. Context's ParameterSet: legitimate absence, not an accidental NULL

Context has no `parameter_set_id`/`parameter_set_ref` in the legacy `StrategySignal` at all (confirmed in the original A7 review, `R9`). P2-A1 represents this as `parameter_set_ref = NULL` **with `parameter_set_status = "LEGACY_IMPLICIT_IN_STRATEGY_ID"`** (`V7`), exactly the architecture's prescribed representation (A7 `03` section 5): an explicit, queryable statement that no separate parameter set exists yet for this strategy, not a silent omission. The same status/field pair is used for Liquidity, where it is equally correct today (each instance is a full `strategy_id`, not yet decomposed into a shared definition + ParameterSet per A3's later model).

## 5. Provenance: three kinds, correctly separated

| Kind | Where it lives | Content | Hashed into `evaluation_id`? |
|---|---|---|---|
| **Source provenance** (strategy-provided) | `Evaluation.provenance` (allowlisted) and `strategy.entry_signals.source_provenance` | `classification`, `gap_recovery`, `source_config_hash`, `source_strategy_fingerprint`, `source_process`, `source_data_age`, `source_market_data_timestamp`, `orchestrator_freeze_timestamp` | **yes** (the allowlisted subset only) |
| **Ingestion provenance** (where/when P2-A1 read the record) | `strategy.entry_signals.source_ref` (`{source_id, source_offset}`) | tailer's file identity and byte offset | **no** |
| **Runtime provenance** (what ingested it) | `strategy.entry_signals.runtime_provenance` | `runtime_version` (`legacy-signal-tailer.v1`), `evaluator_version` (`p2-a1-signal-ingest.v1`) | **no** (both are in `Evaluation.to_dict()` as top-level fields, but `runtime_provenance` as stored on `entry_signals` is a separate, redundant copy for convenient querying) |
| **Evidence provenance** | not yet applicable (no observations exist in P2-A1) | - | - |

Source provenance is **not rewritten** by ingestion: it is copied through the allowlist filter (a subset, never an addition or mutation of values) and is identical whether ingested once, twice, from a copy, or from a different path (`V1` proves this indirectly: if provenance were mutated per-ingest, the hash would not be stable). Normalisation (`decision_time` -> canonical UTC form) is deterministic and pure - the same input always produces the same output (`V8`), and different-but-equivalent inputs (e.g. `-02:00` offset vs `Z`) normalise to the same canonical string, confirmed by direct test.

## 6. `decision_time` semantics preserved

`decision_time` remains the **strategy's reference fill time** (unchanged extraction logic: `raw.decision_time or raw.signal_timestamp or raw.created_at`), exactly the A7-required semantics. `signal_emitted_at` (orchestrator discovery time) remains **separately available** as its own column (`strategy.entry_signals.signal_emitted_at`), not collapsed into `decision_time` and not lost as it was pre-A1 (A7 finding, `R6`: previously it existed only as the outbox `occurred_at`, unqueryable from any strategy table).
