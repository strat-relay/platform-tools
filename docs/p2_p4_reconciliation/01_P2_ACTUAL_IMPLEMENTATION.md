# 01 - What P2 actually implements (commit `8f49aef`)

Reviewed from source, not from the handoff text: `migration/signal.py`, `migration/signal_shadow.py`, `migration/tailer.py`, `migration/reconcile.py`, `migration/gates.py`, `postgres/migrations/010_signal_lifecycle.sql` (+ `008`), `postgres/foundation.py`, `orchestration/storage.py`, `infrastructure/messaging/{contracts,jetstream}.py`, `tests/test_signal_migration.py`, the V1.1 `Evaluation` model. Check ids `R1`-`R25` refer to `tools/verify_p2_p4_reconciliation.py` (25/25 pass; several *confirm defects*). Tests: the 9 P2/substrate tests pass.

## 1. What P2 is

A dormant **S0 shadow path**: `LegacySignalTailer` (`AppendOnlyTailer` over the legacy append-only signal output) -> `canonical_signal(raw)` -> `ingest_signal(conn, signal)`, one PostgreSQL transaction writing `platform.strategy_versions` (if absent), `strategy.candidates`, the V1.1 evaluation family (`persist_evaluation`), `strategy.signals`, and two `platform.outbox_events`. A `SignalShadowConsumer` claims `platform.inbox_events` on receipt of an event and has no other effect. `evaluate_signal_gates` reports the seven A4 gate names, marking authority/retirement gates `NOT_EVALUATED`. There is **no outbox relay** (`R18`), **no real legacy-vs-database reconciliation harness** (`R20`), and **no `SIGNAL_*_PRIMARY_ENABLED` flags** (`R21`). Legacy files remain authoritative. One production line changed: `tradeability_decisions` is now declared in `OrchestrationStore.paths` (OD-01 repair, `R14`, `R16`); no frozen, execution, Trade Manager or bridge file changed (`R15`).

## 2. Canonical identities and relationships implemented

| Concept | Implementation | Stable across restart / duplicate / environment? |
|---|---|---|
| **EntrySignal identity** (`signal_id`) | the legacy `signal_id` passed through **verbatim** (`raw["signal_id"]`); P2 never derives it (`R11`). It is the legacy `stable_id("SIG", identity)` with identity = strategy id, version, instance, source event id (+ economic/opportunity ids for Context). `strategy.signals.signal_id` is the PK | **yes** (deterministic legacy id; duplicates absorbed by `ON CONFLICT (signal_id) DO NOTHING`) |
| **Candidate identity** (`candidate_id`) | `raw.candidate_id` if present, else `stable_id("CAND", {strategy_id, strategy_version, strategy_instance_id, source_event_id, entry_opportunity_id})` (`R24`); `strategy.candidates` PK | **yes** |
| **Evaluation identity** (`evaluation_id`) | `= evaluation_hash = sha256(canonical_bytes(Evaluation.to_dict()))`; `strategy.evaluations` PK and UNIQUE `canonical_hash` | **NO** - the hashed provenance contains `legacy_source_reference` (`"<absolute path>:<line>"`), `legacy_source_hash` and `as_of` (`R12`); the tailer's `source_line` is chunk-relative (`R2`). The same signal ingested from another path, or in another chunking, yields a **different** evaluation hash (`R1`) and therefore an additional orphan evaluation family (evaluation, trace, stage rows), while `strategy.signals` keeps pointing at the first |
| **DecisionTrace reference** | `trace_hash` (in the event) and `strategy.decision_traces(evaluation_id, trace_hash, ...)`; trace is one `legacy_signal` stage, fidelity **L1** for every strategy (Liquidity could be L2 via the V1.1 adapter; P2 records L1) | as stable as the evaluation |
| **Strategy identity** | `strategy_id` verbatim (the four Liquidity instances are four `strategy_id`s; Context one) | yes |
| **StrategyVersion identity** | `strategy_version` = legacy label `"V1"` for Context **and** Liquidity (`R8`); `platform.strategy_versions.strategy_version_id` is set **to the label** with `ON CONFLICT DO NOTHING`, and its `source_hash` is a hash of the *first ingested signal record* (`R7`). So `strategy_version_id = "V1"` is one shared row for different strategies and carries no source identity | **ambiguous** - `(strategy_id, strategy_version)` is the real identity; `strategy_version_id` is not |
| **ParameterSet identity** | `Evaluation.parameter_set_id = raw.get("parameter_set_id")`; `StrategySignal` has no such field, so it is **always NULL** (`R9`). Liquidity's parameters (`entry_fraction`) sit in `strategy_metadata`, which P2 drops | **none** |
| **Instrument identity** | `Evaluation.instrument = canonical_symbol` (`XAUUSD`), falling back to `symbol`/`broker_symbol_hint` | yes (`canonical_symbol` is produced by the adapters with `rstrip("m")`) |
| **`decision_time` semantics** | `raw.decision_time` or `signal_timestamp` or `created_at`; for both adapters `decision_time` is the **strategy's reference fill time**, not discovery. Strings are **not normalised** (`R10`); only `datetime` objects are converted to UTC `Z` form | value preserved; representation not canonical |
| **Discovery / emission time** | `signal_emitted_at` (orchestrator discovery) is used **only** as the outbox event `occurred_at` (`R6`); it is not stored on any strategy table | not queryable from the DB except through the outbox |
| **Source / provenance semantics** | raw `provenance` copied; keys `outcome, future_return, pnl, exit, fill` removed; `legacy_source_reference`, `legacy_source_hash`, `as_of` **added** (`R12`) | mutated and hash-unstable |
| **Runtime provenance** | `runtime_version = "legacy-signal-tailer.v1"`, `evaluator_version = "p2-signal-ingest.v1"` (hashed); no process/pod/image identity recorded | not recorded |
| **Evaluation hash** | as above | see Evaluation identity |
| **Terminal state** | `lifecycle_state = 'ENTRY_SIGNAL_CREATED'` on both candidate and signal (`R23`); the CHECK allows `CANDIDATE_DETECTED, EVALUATED, REJECTED, ENTRY_SIGNAL_CREATED`; P2 writes only the last | yes |
| **Event identity** | `event_id = "<candidate_id>:candidate.detected"` and `"<signal_id>:entry.created"`; `aggregate_type = 'signal'`, `aggregate_id = signal_id` for **both** (the candidate event is filed under the signal aggregate), `aggregate_version = 1`, `schema_version = 'event-envelope.v1'`, `occurred_at` per above | yes (deterministic; `ON CONFLICT (event_id) DO NOTHING`) |
| **Event payload** | `{signal_id, candidate_id, evaluation_id, evaluation_hash, trace_hash, source_reference}` - identical for both event types (`R5`) | as stable as the hashes and `source_reference` |
| **Persisted EntrySignal content** | `strategy.signals.payload` = `Evaluation.to_dict()`; **no entry/stop/target/risk, no economic_position_id, entry_opportunity_id, setup_id, strategy_instance_id, entry mechanism, signal_emitted_at** (`R4`, `R25`); only `symbol/canonical_symbol/entry_type/timeframe/lower_timeframe/higher_timeframes` survive, in stage metadata | **information lost** relative to `orchestration.models.StrategySignal` |

## 3. Restart, duplicate, rotation, malformed behaviour (tailer)

| Scenario | Behaviour | Evidence |
|---|---|---|
| partial last line | not consumed until its newline arrives | `test_s0_tailer_restart_partial_malformed_and_rotation` |
| restart | offset + prefix sha256 in a checkpoint **file**; ingest runs before the checkpoint write, so a crash mid-chunk replays the chunk (at-least-once); replays are absorbed for signal/candidate/event ids but create extra evaluations if `source_line` differs (`R1`, `R2`) | source |
| rotation/truncation | detected by offset > size or prefix-hash mismatch; restart from offset 0 | test |
| malformed complete line | **consumed** and reported only in that run's `TailerResult.malformed`; nothing persisted (`R3`) | behaviour |
| DB failure during ingest | exception propagates before the checkpoint write => replay | source |
| checkpoint file | is required by the tailer (A4's "no checkpoint file required for correctness" holds only via idempotent ids, which `R1` weakens) | source |

## 4. Envelope and transport facts P2 inherits from V1.2

* `EventEnvelope` fields: `event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at, payload, correlation_id, causation_id, producer, schema_version`.
* `JetStreamPublisher.publish` publishes `canonical_bytes` to a subject equal to `event_type` with **no `Nats-Msg-Id`/headers**; `validate_subject` accepts only exact members of the V1.2 subject set, so **instrument-token subjects are refused** (`R17`). No `trade.*` / management subject exists (`R19`).
* The relay from `platform.outbox_events` to JetStream does not exist (`R18`); P2's JetStream gate evidence came from an isolated harness.

## 5. Summary of P2 correctness against its own purpose

P2 delivers what it claims: dormant, idempotent-by-signal-id shadow ingest with atomic outbox, no execution coupling and no frozen change. It **does not** yet provide (a) a stable Evaluation identity, (b) a persisted EntrySignal record with the geometry and legacy references that downstream consumers need, (c) a unique StrategyVersion/ParameterSet reference, (d) a durable record of malformed lines, (e) a relay, or (f) a real legacy-versus-database reconciliation. (a)-(c) block `P4.2`; (a), (d)-(f) block meaningful `P2.1` live-shadow evidence (see `12`).
