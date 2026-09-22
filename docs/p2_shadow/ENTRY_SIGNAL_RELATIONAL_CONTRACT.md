# P2 EntrySignal persistence and cutoff contract

## Entry mechanisms

The active `ContextStructureRetrace` producer accumulates independently
observed mechanism facts and applies `sorted(set(...))`: `DEPTH_ONLY`,
`REJECTION_WICK`, `LOWER_TF_ENGULFING`, and `MORNING_EVENING_STAR`. The enabled
`LiquidityDisplacement` adapter emits a singleton mechanism selected from its
`fill_confirmation` value (default `COMPLETED_CANDLE_RETRACEMENT`). Disabled
liquidity-instance plumbing emits `LIQUIDITY_RECLAIM`. The producer's
`StrategySignal` keeps a tuple under its existing singular wire key; JSON
serialization represents that value as an array. The ingest boundary maps
that current array contract into canonical `entry_mechanisms` and never
accepts a scalar.

Mechanisms are set-like facts, not an ordered sequence: ContextStructureRetrace
deduplicates and lexically sorts them, and other active producers emit one
item. Canonicalization therefore rejects duplicates and sorts non-empty text
tokens lexically. Empty is permitted by the producer's tuple/list domain;
`null` is not equivalent and is rejected. There is no shared vocabulary
registry or per-mechanism lifecycle/metadata today, so constrained `text`
values in a relational child table are preferable to a reference table or a
migration-heavy PostgreSQL enum. Strings are strict values, not arbitrary JSON.

`strategy.entry_signals` is the parent. `strategy.entry_signal_mechanisms`
stores one child row per mechanism with an explicit zero-based `position`.
`(entry_signal_id, mechanism)` prevents duplicates, `(entry_signal_id,
position)` makes reconstruction deterministic, and the FK enforces parent
integrity. Parent, child rows, and outbox events are written in one database
transaction. The transport event remains `signal.entry.created.v1` and
serializes the ordered canonical collection as a JSON array; this is wire
serialization, not PostgreSQL JSON storage. No live canonical event has been
published from this deployment, so a pre-cutoff payload correction does not
require a version bump.

`entry_signal_hash` includes the canonical collection. Evaluation identity
does not: an `Evaluation` models the decision trace, while mechanisms are
EntrySignal semantics. `signal_id`, `candidate_id`, and event IDs remain
opaque producer/identity-derived values and are not rewritten. Therefore a
mechanism change changes EntrySignal semantic identity/hash, not these stable
IDs or the Evaluation hash.

## Relational-first audit (bounded to the P2 signal contract)

| Field | Current storage | Stable domain semantics / queryability | Classification | Rationale |
|---|---|---|---|---|
| `entry_mechanisms` | Child rows in `strategy.entry_signal_mechanisms` | Yes; filter/index/order/integrity | `RELATIONALIZE_NOW` | Independent mechanism membership and ordering are canonical facts. |
| `entry_price`, `stop_price`, `risk_distance`, `target_price`, `target_distance`, `target_r` | Numeric parent columns | Yes | `JUSTIFIED_RELATIONAL` | Already modeled as queryable signal geometry. |
| `strategy_ref`, strategy/version, parameter-set reference/status | Text parent columns | Yes | `JUSTIFIED_RELATIONAL` | Stable identities/references; parameter-set FK needs a future definition registry decision. |
| `source_id`, `source_offset`, `evidence_class`, `cutoff_id` | Relational parent columns (migration 012) | Yes; scope/filter/reconcile | `RELATIONALIZE_NOW` | Cutoff-aware ingestion/reconciliation must query these fields. |
| `source_provenance` | JSONB | Mixed: provenance keys are useful, but payload is strategy/provider-specific | `FUTURE_REVIEW` | Keep bounded; define stable provenance columns only with a phase contract. |
| `strategy_metadata` | JSONB | Strategy-specific extension fields; some may later be reportable | `FUTURE_REVIEW` | No common stable schema has been approved; don't broaden this migration. |
| `runtime_provenance` | JSONB duplicate of Evaluation runtime/evaluator versions | Stable, already relational in `strategy.evaluations` | `RELATIONALIZE_NOW` | Remove duplicate JSON column; the canonical Evaluation FK owns these values. |
| `source_ref` / ingestion provenance | JSONB | Stable source/cursor/cutoff fields | `RELATIONALIZE_NOW` | Replaced by relational provenance columns in migration 012. |
| DecisionTrace canonical payload | JSONB plus `decision_traces`, `stage_results`, reason-code relations | Internal trace is immutable evidence; selected stages/reasons are relational | `JUSTIFIED_JSON` | Keep the full trace snapshot for evidence/replay; normalized stage and reason tables support queries/integrity. |
| Reason codes | `platform.reason_codes` and `strategy.evaluation_reason_codes` | Yes | `JUSTIFIED_RELATIONAL` | Already normalized and FK-constrained. |
| Event/outbox payloads | JSONB in PostgreSQL; ordered JSON array on wire | Transport/forward-compatible envelope | `JUSTIFIED_JSON` | Event payload is a transport contract, not canonical relational storage. |
| Candidate and generic signal payloads | JSONB projections | Mixed/duplicative | `FUTURE_REVIEW` | Mechanisms are excluded from these generic payloads; audit remaining duplication separately. |
| Quarantine raw payload and error | Raw text + diagnostic text | Opaque source evidence | `OPAQUE_EVIDENCE` | Preserve malformed input exactly for investigation; not canonical state. |
| Evaluation canonical payload / trace snapshot | JSONB alongside normalized Evaluation columns and child relations | Immutable canonical evidence snapshot | `JUSTIFIED_JSON` | Relational decision/identity fields remain authoritative; snapshot supports deterministic audit. |
| ParameterSet definitions and references | No ParameterSet relation in current migrations; reference is text | Definition identity should eventually be FK-queryable | `FUTURE_REVIEW` | No approved registry/seed contract exists in this bounded correction. |

Only the mechanism collection and stable source/cutoff fields are
relationalized now. The broader JSON fields remain explicit future review,
opaque evidence, or transport data rather than being mechanically rewritten.

## Cutoff and migration invariant

`MIGRATE_CONFIGURATION_NOT_RUNTIME_HISTORY`: configuration/definitions may
be seeded when the new runtime requires them; operational stores begin
population at an explicit persisted authority/cutoff boundary. Historical
runtime data is not imported merely to populate the new runtime. A future
phase that needs current active state for safe continuity requires its own
atomic cutover snapshot/reconciliation contract; that is not historical
runtime migration.

For P2, historical signals, evaluations, outbox events, and checkpoints are
not imported. The prior 81 quarantine rows remain `PRE_CUTOFF_DIAGNOSTIC`,
not canonical runtime history and not P2.1 evidence. They are preserved and
excluded from runtime quarantine metrics. First worker initialization
persists the source identity, EOF cursor, cutoff UTC/ID, final complete
pre-cutoff signal metadata, deployment/configuration provenance, and a
durable checkpoint. Restart uses the persisted marker/checkpoint; it never
resamples EOF. Source rotation/prefix replacement after cutoff fails closed
to prevent a replay from byte zero. Reconciliation reads only source offsets
at or after the marker and matching relational source identity.

The repository's P2 code is prepared for this boundary. No real evidence
boundary, worker attachment, Kubernetes change, or authority change is made
by this code correction.
