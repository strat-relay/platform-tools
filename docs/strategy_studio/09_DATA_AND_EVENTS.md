# V1 PostgreSQL ownership and events

Design only: no migration, table or stream is created.  Placement follows ADR-0001 D14:
Strategy Studio lives in the **`trading`** database, inside the Strategy context, and must
not depend on `execution`, `commerce`, MT5, or any customer data.

## 1. Two schemas, two planes

| Schema | Plane | Character |
|---|---|---|
| **`strategy`** | registry + runtime artefacts | mostly **immutable, content-addressed**; consumed by runtimes, orchestration, Console |
| **`studio`** | authoring | conversational and workflow state; **append-only logs** plus a few mutable workflow rows |

(ADR-0001 already assigns `strategy.*` to the Strategy context; `studio.*` is a new schema
in the same database and owned by the same context.)

## 2. `strategy` schema

| Table | Purpose | Key columns | Mutability |
|---|---|---|---|
| `strategy` | the idea/container | `strategy_key` PK, name, intended mode, owner | **mutable metadata** |
| `definition` | canonical rule document | `definition_hash` PK, `schema_version`, `evaluation_mode`, `canonical_json` | **immutable** |
| `parameter_set` | values for a definition | `parameter_set_hash` PK, `definition_hash`, `instrument?`, `values` | **immutable** |
| `primitive` | registry identity | `primitive_id` PK, family, kind | immutable |
| `primitive_version` | one immutable primitive release | (`primitive_id`,`version`) PK, `spec`, `implementation_ref`, `impl_hash`, `test_report_hash` | **immutable** (status in the history table) |
| `primitive_status_history` | `EXPERIMENTAL/APPROVED/DEPRECATED/WITHDRAWN` | version, status, reason, actor, at | **append-only** |
| `reason_code` | reason-code registry | code, kind, severity, template, audience, `version` | immutable per version |
| `execution_plan` | compiled plan | `plan_hash` PK, `definition_hash`, `parameter_set_hash`, `engine_version`, `plan`, `compile_report` | **immutable** |
| `strategy_version` | the evidence-bearing release | `strategy_version_id` PK, `strategy_key`, `definition_kind` (`DECLARATIVE`\|`LEGACY_PYTHON`), `definition_hash?`, `parameter_set_hash?`, `plan_hash?`, `engine_version?`, `legacy_identity?`, `derived_from?`, `freeze_manifest`, `frozen_at` | **immutable** |
| `version_primitive_pin` | pins | `strategy_version_id`, `primitive_id`, `version`, `impl_hash` | immutable |
| `version_status_history` | lifecycle | version, from, to, `promotion_record_id`, at | **append-only** |
| `promotion_policy` | thresholds | `policy_id`, `version`, `criteria` | immutable per version |
| `promotion_record` | one promotion decision | record id, version, transition, `criteria_snapshot` (values vs thresholds), waiver?, approver, role, reason, at | **append-only** |
| `candidate` | in-flight/terminal candidate | `candidate_id`, `strategy_version_id`, instrument, direction, `state`, `evaluation_id` | **mutable state** with history table; identity immutable |
| `evaluation` | one Evaluation + trace (§4 of `05_…`) | `evaluation_id` PK, `strategy_version_id`, `plan_hash`, instrument, `as_of`, direction, `outcome`, `terminal_reason`, `tier`, `trace jsonb?`, `trace_hash`, `inputs_digest`, `run_id?`, `recorded_at` | **immutable, append-only**, partitioned by month; `ON_DEMAND` rows store hash only |
| `review_task` | HYBRID gate instance | task id, evaluation/candidate, gate id, question, `deadline`, `status` | **mutable status** |
| `review_answer` | the human's answer | task id, answer, reasons, actor, at | **immutable** |

`strategy_version` rows for `LEGACY_PYTHON` carry `legacy_identity` = the existing
`{code_hash, decision_code_hash, config_hash}` **verbatim** — no re-hashing.  They map to
today's `platform.strategy_versions/freeze_manifests` (ADR-0001 `05_…` §5).

## 3. `studio` schema

| Table | Purpose | Mutability |
|---|---|---|
| `draft` | authoring lineage; `authoring_state` (`DRAFT…BACKTESTED`), `current_revision_id`, `lineage_id` | **mutable** pointer/state |
| `draft_revision` | each edit → definition hash, parent, note, actor | **immutable** |
| `session`, `message` | authoring conversation, incl. AI turns (`model_id`, `prompt_hash`, tool calls) | **append-only** |
| `proposal` | AI proposals (structured patches) | content immutable; `status` mutable |
| `example_set`, `example` | chart examples: label, `partition_role`, decision time, `evidence_kind`, observed geometry | core **immutable**; review status separate |
| `image_asset` | content-addressed image metadata (blob in object storage) | immutable |
| `annotation` | marks (versioned), `space`, calibration, `origin` | immutable versions |
| `market_anchor`, `anchor_verification` | bridge to canonical data; user confirmation events | anchor immutable; verification append-only |
| `statement`, `term` | verbatim utterances; extracted terms | immutable |
| `ambiguity`, `ambiguity_status_history` | discovered gaps | identity immutable; status append-only |
| `interpretation`, `clarification_decision` | proposed operationalisations; the act of accepting/rejecting (exact patch applied) | interpretation immutable; decision **append-only** |
| `dataset_partition` | partition spec, sampling seed, `sealed_at` (one-way) | spec immutable; `sealed` one-way |
| `partition_exposure` | exposure ledger (lineage, purpose, at) | **append-only** |
| `validation_set`, `validation_case` | sealed set; cases with stratum, `decision_time`, `snapshot_hash` | immutable once sealed |
| `human_decision` | answer (`YES/NO/NEED_MORE_CONTEXT`), mode, reasons, disclosures, first-impression vs informed | **immutable append-only** (a correction is a new row) |
| `engine_decision` | engine outcome for a case → `evaluation_id` | immutable |
| `validation_round_result` | computed metrics snapshot per round | immutable snapshot (recomputable) |
| `backtest_run`, `backtest_run_summary`, `control_run` | run spec/status; funnel and results; ablation runs | spec immutable; `status` mutable; summary immutable |
| `primitive_request` | workflow for new primitives | mutable workflow |

## 4. Immutability enforcement

* **Permissions:** the application role has `INSERT`/`SELECT` only on immutable and
  append-only tables (no `UPDATE`/`DELETE`); mutable-status tables are updated through
  narrow functions that also write the history row.
* **Content addressing:** `definition`, `parameter_set`, `execution_plan` are keyed by
  hash; an insert with an existing hash is a no-op, a hash collision with different
  content fails.
* **One-way flags:** `sealed_at` on partitions/sets can be set once; triggers reject
  clearing it.
* **Read isolation:** the author/analyst database role cannot `SELECT` sealed partition
  data or `VALIDATION_UNTOUCHED` case content; a `SECURITY DEFINER` evaluation function
  is the only reader and writes `partition_exposure` on every call (`07_…` §4).
* **Cross-plane references are by hash/id** (`definition_hash`, `strategy_version_id`);
  `studio` may reference `strategy`, never the reverse (the registry must not depend on
  authoring conversations to run).

## 5. What is deliberately **not** stored in PostgreSQL as source of truth

Image and chart-render blobs (object storage, hash in DB); bulk backtest bar data and
Parquet (research/export format per ADR-0001); LLM provider logs beyond the message rows.

## 6. V1 events

Only events that another process must react to are on the bus.  Everything else is a
**database audit record** (already captured by the append-only tables above).

| Event | Producer | Consumers | Transport | Why |
|---|---|---|---|---|
| `strategy.version.frozen` | Studio | runtime supervisor (may load the plan), Console read model | **JetStream** + DB | a runtime must learn a new version exists |
| `strategy.promoted` | Studio | runtime supervisor (start/stop `PAPER`/`SHADOW`/`ACTIVE` instances), orchestration, Console | **JetStream** + DB | changes where signals flow |
| `strategy.primitive.withdrawn` | Registry | runtime supervisor (demote/flag affected versions), Console | **JetStream** + DB | safety-relevant, must not be missed |
| `strategy.candidate.detected` | Runtime | Console review queue (`requires_review` for HYBRID) | **JetStream** + DB | drives review tasks; bounded volume (candidates, not evaluations) |
| `strategy.review.completed` | Console/Studio | Runtime (resumes the evaluation) | **JetStream** + DB | resumes a paused HYBRID evaluation |
| `signal.entry.created` | Runtime → Signals | orchestration (V1 shadow routing), performance ingest | **JetStream** + DB (ADR-0001 `04_…`) | the single boundary event out of the Strategy context |

**Database audit only (no bus):** `strategy.draft.created`, `strategy.definition.updated`
(= a `draft_revision` row), `strategy.example.added`, clarification decisions,
`strategy.validation.completed` (metrics snapshot; the Console reads the DB), backtest run
lifecycle, primitive status changes other than withdrawal, and
**`strategy.decision.recorded`** — every Evaluation is a row in `strategy.evaluation`;
publishing each on JetStream would put per-bar, per-instrument noise on the bus for no
consumer.  Only the *interesting* outcomes (candidate, signal) are events.

Envelope, ordering and outbox/inbox rules are ADR-0001's (`docs/architecture/04_DOMAIN_EVENTS.md`
§1–§2): events are written to `platform.outbox` in the same transaction as the state
change; consumers keep an inbox; ordering key `strategy_version_id + instrument`.

None of these events carries broker, account, customer or MT5 fields.
