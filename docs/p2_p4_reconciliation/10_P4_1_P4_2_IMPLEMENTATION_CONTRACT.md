# 10 - Implementation contract for P4.1 (contracts/schema) and P4.2 (ManagedTrade shadow)

For Codex. Architecture is fixed by `03`-`09`; nothing here requires reinterpretation. **Not implemented by A7.** Names of modules are proposals that follow existing repository conventions (`migration/`, `postgres/migrations/NNN_*.sql`, `infrastructure/messaging/`, `tests/`).

**Neither stage may require**: MT5 execution, broker writes, broker-state authority, the execution consumer, any Trade Manager action policy, customer delivery, entitlement, Phase 7, or any legacy `trade_manager/` import. Both run with **no broker or bridge credentials present**.

**Prerequisite for P4.2 (not P4.1): P2-A1** (`12`) - the EntrySignal canonical record and stable evaluation identity.

---

## P4.1 - contracts and schema

### Deliverables

| Area | Content |
|---|---|
| New package `trade_management/` (do **not** modify or import legacy `trade_manager/`) | `ids.py` (`managed_trade_id`, `observation_id`, `market_snapshot_id`, `decision_id`, `tm_version_id` derivations), `versions.py` (manifest schema, canonicalisation, hash tool, `TM-NONE-1`/`TM-LEGACY-0` definitions), `contracts.py` (event payload dataclasses/validators for `trade.opened.v1`, `trade.observation.recorded.v1`, `trade.decision.made.v1`, `trade.closed.v1`), `binding.py` (`StreamBindingResolver` protocol only + `LegacyStaticResolver` + `DefaultTmNoneResolver` interfaces), `states.py` (ManagedTrade state machine table) |
| Messaging (`infrastructure/messaging/`) | add subjects `trade.opened.v1`, `trade.observation.recorded.v1`, `trade.decision.made.v1`, `trade.closed.v1`, `signal.management.published.v1`; `STREAMS`: extend `TRADING_CORE` with the **explicit** trade/management subjects (no `trade.>`), add **`TRADING_OBSERVATION`** = `trade.observation.recorded.v1` only (file storage, `limits`, `discard old`; **`max_age`/`max_bytes` are OD-08: expose as configuration with no production default committed**); **`JetStreamPublisher.publish` must pass `Nats-Msg-Id = envelope.event_id`** (headers pass-through) - existing tests unchanged |
| Migration `postgres/migrations/011_trade_management_contracts.sql` (+ `DATABASE_SCHEMA_VERSION = "011"`) | tables below; legacy stubs `trade_management.observations/decisions/checkpoints` (003) are **left in place and unused** (documented deprecated; not dropped) |
| Golden vectors | `TM-NONE-1` manifest hash; `managed_trade_id` for fixed `signal_id`s; `observation_id`, `decision_id` for fixed inputs |
| Docs | contract reference generated from the dataclasses (single source) |

### Tables (schema `trade_management`)

| Table | Key columns and constraints |
|---|---|
| `trade_manager_version` | `tm_version_id PK`, `label`, `manifest jsonb NOT NULL`, `manifest_hash text NOT NULL UNIQUE`, `frozen_at timestamptz NOT NULL`, `derived_from text NULL REFERENCES trade_manager_version`, `status text CHECK (status IN ('FROZEN','RETIRED'))`. **Immutability trigger**: any UPDATE except `status FROZEN->RETIRED` raises; DELETE raises. `CHECK (tm_version_id = 'TMV_' || left(manifest_hash, 24))` |
| `tm_version_promotion` (append-only) | `promotion_id PK`, `tm_version_id FK`, `publication_eligibility CHECK IN ('SHADOW_ONLY','PUBLISHABLE')`, `decided_by`, `decided_at`, `evidence_ref` |
| `legacy_stream_binding` (append-only) | `binding_id PK`, `strategy_id NOT NULL`, `strategy_instance_id NULL`, `instrument NULL`, `tm_version_id FK NOT NULL`, `market_feed_id NULL`, `valid_from NOT NULL`, `binding_hash NOT NULL`, `UNIQUE (strategy_id, coalesce(strategy_instance_id,''), coalesce(instrument,''), valid_from)`; INSERT-only |
| `managed_trade` | `managed_trade_id PK`, `entry_signal_id text NOT NULL UNIQUE REFERENCES strategy.signals`, `entry_signal_hash NOT NULL`, `signal_stream_id NULL`, `strategy_id`, `strategy_version`, `strategy_ref NOT NULL`, `strategy_version_id NULL`, `parameter_set_ref NULL`, `parameter_set_status`, `instrument`, `direction CHECK IN ('LONG','SHORT')`, `decision_time timestamptz NOT NULL`, `reference_entry_price`, `initial_stop`, `initial_target`, `risk_distance CHECK (risk_distance > 0)`, `reference_entry_semantics NOT NULL`, `tm_version_id NOT NULL REFERENCES trade_manager_version`, `tm_binding_id`, `binding_hash`, `tm_bound_at`, `binding_resolution CHECK IN ('STREAM','LEGACY_STATIC','DEFAULT_TM_NONE')`, `market_feed_id NULL`, `evidence_mode CHECK IN ('FORWARD','REPLAY','BACKTEST')`, `eligibility`, `eligibility_reason NULL`, `creation_lag_seconds`, `legacy_refs jsonb`, `record_mode CHECK IN ('SHADOW','PRIMARY')`, `state CHECK IN ('PENDING_ENTRY','OPEN','CLOSED','CANCELLED')`, `last_applied_seq bigint NOT NULL DEFAULT 0`, `mfe_r`, `mae_r`, `version bigint NOT NULL DEFAULT 1`, `created_at timestamptz NOT NULL DEFAULT now()`, **`UNIQUE (managed_trade_id, tm_version_id)`** (target of the decision FK). **Binding-immutability trigger** over `entry_signal_id, entry_signal_hash, strategy_ref, tm_version_id, tm_binding_id, binding_hash, tm_bound_at, decision_time, reference_*`, `initial_*`, `evidence_mode` |
| `managed_trade_state_history` | `(managed_trade_id, version) PK`, `from_state`, `to_state`, `reason`, `at`; allowed transitions enforced by a function (`OPEN`->`CLOSED`, `PENDING_ENTRY`->`OPEN|CANCELLED`) |
| `managed_trade_skip` | `entry_signal_id PK`, `reason`, `detail`, `at` - signals for which no ManagedTrade could be created (never silent) |
| `market_snapshot` | `market_snapshot_id PK`, `provider_id`, `feed_id`, `instrument`, `source_timestamp`, `bid`, `ask`, `spread`, `data_status`, `quote_hash`; `CHECK (ask >= bid)` |
| `trade_observation` | `observation_id PK`, `managed_trade_id FK`, `observation_seq bigint`, `UNIQUE (managed_trade_id, observation_seq)`, `market_snapshot_id FK`, `observed_at`, `effective_at`, `bars_ref jsonb`, `tm_version_id`, `data_status`, `payload_hash`; `CHECK (observation_seq >= 1)` |
| `trade_manager_decision` | `decision_id PK`, `managed_trade_id`, `tm_version_id`, **`FOREIGN KEY (managed_trade_id, tm_version_id) REFERENCES managed_trade(managed_trade_id, tm_version_id)`**, `observation_id FK UNIQUE per (observation_id, tm_version_id)`, `observation_seq`, `evaluation_track CHECK IN ('BOUND','CHALLENGER')`, `action CHECK IN ('HOLD','MOVE_STOP','MOVE_TO_BREAKEVEN','TRAIL_STOP','PARTIAL_PROFIT','EXIT')`, `parameters jsonb`, `reason_codes text[]`, `decision_trace_ref jsonb`, `decision_time`, `persisted_at timestamptz NOT NULL DEFAULT now()`, `data_status`, `record_mode` |
| `managed_trade_evaluation_track` | `(managed_trade_id, tm_version_id) PK`, `role CHECK IN ('BOUND','CHALLENGER')` (BOUND row mirrors the ManagedTrade; CHALLENGER rows are separate series) |

**DDL property tests (mandatory)**: no column named or typed like `account*`, `ticket*`, `lot*`, `volume*`, `broker_position*`, `execution_*`, `entitle*`, `subscription*`, `customer*`, `published_*` (except a nullable future `published_signal_ref` if and only if added by the Signals domain) in any `trade_management` table; the only foreign keys to other schemas are `strategy.signals`.

### Events (subjects and payload references)

| Subject (stream) | `event_id` | Envelope | Payload (small facts + references) |
|---|---|---|---|
| `trade.opened.v1` (`TRADING_CORE`) | `"<managed_trade_id>:trade.opened"` | `aggregate = managed_trade/<id>`, `aggregate_version = 1`, `correlation_id = signal_id`, `causation_id = the signal.entry.created event_id` | `managed_trade_id, entry_signal_id, entry_signal_hash, strategy_ref, instrument, direction, decision_time, reference_entry_price, initial_stop, initial_target, tm_version_id, tm_binding_id, evidence_mode, eligibility, record_mode` |
| `trade.observation.recorded.v1` (`TRADING_OBSERVATION`) | `observation_id` | `aggregate_version = observation_seq` | `A6 08` payload (no account/ticket/lot) |
| `trade.decision.made.v1` (`TRADING_CORE`) | `decision_id` | `aggregate_version = observation_seq` | `A6 11` model |
| `trade.closed.v1` (`TRADING_CORE`) | `"<managed_trade_id>:trade.closed"` | | both outcomes (later stages) |

Outbox/inbox: **reuse** `platform.outbox_events` / `platform.inbox_events`; consumer names `trade-mgmt-open` (P4.2), `trade-manager-shadow` (P4.4). Event ids are deterministic so a re-run regenerates the same id.

### P4.1 acceptance tests

1. manifest hash determinism and golden vector for `TM-NONE-1`; any manifest member change changes the id; excluded members do not.
2. `managed_trade_id`, `observation_id`, `decision_id` golden vectors; ids independent of process, path, wall clock.
3. DDL: immutability triggers (UPDATE of each protected column raises), composite FK rejects a decision under another version, `UNIQUE (managed_trade_id, observation_seq)`, `ask >= bid`, forbidden-column property test, allowed state transitions only.
4. contract tests: every event payload validates; unknown `schema` rejected; no forbidden field present.
5. messaging: `validate_subject` accepts the new exact subjects and still refuses tokenised/unknown ones; `TRADING_OBSERVATION` and `TRADING_CORE` subject sets do not overlap; publisher sets `Nats-Msg-Id`.
6. import audit: `trade_management` imports nothing from `execution`, `live_execution_consumer`, `trade_manager`, `contracts.mt5_bridge`, `orchestration`, `control_api`, `context_structure_retrace_phase7_observer`.
7. migration applies idempotently on the isolated dedicated PostgreSQL; `DATABASE_SCHEMA_VERSION` consistent.
8. no runtime, broker, or file-fallback path: the package constructs no JSONL store and reads no runtime directory.

---

## P4.2 - ManagedTrade shadow

### Deliverables

| Module | Content |
|---|---|
| `trade_management/managed_trade.py` | `create_managed_trade(conn, entry_signal_record, resolver, *, now_utc) -> CreationResult` (pure decision + one transaction), eligibility computation, skip recording |
| `trade_management/binding.py` | `LegacyStaticResolver` reading `legacy_stream_binding`; `DefaultTmNoneResolver` fallback to `TM-NONE-1`; resolver output includes `binding_hash` |
| `trade_management/open_consumer.py` | durable consumer `trade-mgmt-open` on `signal.entry.created.v1` (`TRADING_CORE`), handler = `create_managed_trade`; **shadow**: `record_mode = SHADOW`, no other effect |
| `trade_management/reconcile.py` | reports signals without a ManagedTrade/skip row (an **anti-join report**, not a work queue) and ManagedTrades whose `entry_signal_hash` no longer matches; findings persisted via P0/P1 `platform.reconciliation_*` |
| Seeds | `TM-NONE-1`, `TM-LEGACY-0`, and one `legacy_stream_binding` row per known strategy id -> `TM-NONE-1` (data migration, reviewed) |

### Behaviour

**Creation transaction** (single `BEGIN..COMMIT`): claim inbox `(trade-mgmt-open, event_id)` -> load `strategy.entry_signals` by `signal_id` (P2-A1) -> verify `entry_signal_hash` -> compute eligibility (`provenance_class`, `creation_lag`, `L_create` from configuration - default **unset = ELIGIBILITY_UNEVALUATED**, never a guessed number) -> resolve binding **once** -> insert `managed_trade` (`ON CONFLICT (entry_signal_id) DO NOTHING RETURNING`) -> if conflict, **compare** the stored `entry_signal_hash`: equal = duplicate; different = reconciliation finding + quarantine, never overwrite -> insert state history -> insert outbox `trade.opened.v1` -> mark inbox processed -> commit. Ack after commit.

**State transitions written in P4.2**: create as `OPEN` (both adapters emit post-fill). `PENDING_ENTRY` and `CANCELLED` exist in the schema/state table, unused. No transition to `CLOSED` in P4.2 (no observations/decisions yet; strategy exit events are a later stage).

**Duplicates**: redelivery -> inbox hit; replayed signal event with the same hash -> `ON CONFLICT` no-op; different hash -> finding + quarantine `ENTRY_SIGNAL_MISMATCH`.

**Restart**: no in-memory state; durable JetStream consumer + inbox + the anti-join reconciliation report. A consumer restart or a lost stream is repaired by the reconciler *invoking the same idempotent creation function* (flagged `repair=true` in the outbox/audit), never by polling tables for work.

**Version binding**: `tm_version_id` from the resolver at creation; all binding columns immutable (trigger); the resolver is never called again (test: change `legacy_stream_binding` after creation; the existing trade's binding does not change).

**Backfill of already-ingested shadow signals** (P2 rows): allowed through the same function with `evidence_mode = FORWARD` only if lag policy permits; otherwise `eligibility = FORWARD_INELIGIBLE(LATE_CREATION)`; never `FORWARD` by default.

**Failure semantics**

| Failure | Behaviour |
|---|---|
| EntrySignal record missing | retry with backoff, then quarantine `ENTRY_SIGNAL_RECORD_MISSING`; never create from the event payload alone |
| binding table empty / no row | `DEFAULT_TM_NONE` binding (recorded) - creation never blocks on missing bindings |
| `TM-NONE-1` not registered | **fail closed**: no creation, health `UNAVAILABLE` |
| PostgreSQL unavailable | no ack; JetStream redelivers; no file fallback |
| hash mismatch on duplicate | quarantine + finding |
| unknown event schema | park + alert |

**Observability** (metrics, all read-only): `managed_trade_created_total{eligibility,binding_resolution}`, `managed_trade_skipped_total{reason}`, `entry_signals_without_managed_trade` (from the reconciler), `creation_lag_seconds` histogram, `inbox_duplicate_hits_total`, `outbox_oldest_unpublished_age_seconds`, `quarantine_open`, `tm_version_registered{tm_version_id}`, consumer lag/redeliveries.

**Migration compatibility**: additive only; no change to any P2 table's semantics; runs beside the legacy files; `record_mode = SHADOW` rows are excluded from any authoritative read; `PRODUCTION_FILE_IPC` unaffected; no legacy file read.

### P4.2 acceptance tests

1. one EntrySignal (each adapter shape: Context with `economic_position_id`, Liquidity with `strategy_metadata`) -> exactly one ManagedTrade with the documented columns; ids match golden vectors.
2. **independence**: creation succeeds in a process with no `execution`/`trade_manager`/bridge modules importable and no broker/bridge credentials; DDL has no forbidden columns; an EntrySignal with no `PublishedSignal` and no execution intent yields a ManagedTrade (unpublished + unexecuted cases).
3. duplicate event, redelivery, crash-before-commit (rollback), crash-after-commit-before-ack -> one row, one outbox row, one inbox row.
4. same `signal_id` with different `entry_signal_hash` -> quarantine + finding, original row untouched.
5. **silent-rebinding suite** (`04` section 5, items 1-7).
6. eligibility: `GAP_RECOVERY`, `PRE_ORCHESTRATOR_REFERENCE`, large creation lag -> `FORWARD_INELIGIBLE(reason)`, still created.
7. missing EntrySignal record -> quarantine, no creation; `TM-NONE-1` absent -> fail closed.
8. anti-join reconciliation report finds an injected missing ManagedTrade and repairs it idempotently.
9. shadow isolation: no read of `SHADOW` rows by authoritative queries; no subject other than the shadow consumer's subscription is created.
10. isolated PostgreSQL + JetStream harness: P2 event -> ManagedTrade -> `trade.opened.v1` outbox row (published only if the relay flag is enabled; default off).
11. static audit run (A4 `audit_file_ipc.py`): no new file-IPC candidates; A6/A7 evidence checkers still pass.
