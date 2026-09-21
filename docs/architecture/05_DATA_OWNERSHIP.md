# Data ownership: Trading PostgreSQL vs StratRelay commercial PostgreSQL

Target: **PostgreSQL = authoritative durable state and history; NATS JetStream = durable
operational transport; JSONL/Parquet = research/export/backtest formats only.**
JSONL is *not* a third production persistence system (`04_…` §5 lists each retirement).

## 1. Rules

1. **Two logical databases, one owner each.**  `trading` is owned by `trading-platform`;
   `commerce` is owned by `stratrelay-platform`.  They may start on one PostgreSQL
   *cluster*, but as **separate databases with separate roles**; no role can read the
   other's database.
2. **No cross-database foreign keys, no cross-database joins, no shared tables.**
   References are **opaque ids by value** (`stream_id`, `signal_id`, `managed_trade_id`,
   `snapshot_id`).  Integrity across the boundary is by event + reconciliation job, not
   by constraint.
3. **Commerce keeps its own immutable copies** of what customers see (published
   signals, management signals, outcomes, approved performance) as read models built
   from events.  Customers must still see their history if the trading schema is
   refactored or a stream is retired.
4. **Personal data is contained.**  Broker account identifiers, sizes, tickets, balances
   and reconciliations live only in the `execution` schema of `trading` under a
   dedicated role, and are never emitted to Commerce (event privacy rules, `04_…` §1.5).
   Customer PII lives only in `commerce.identity`.
5. **Each database owns its outbox and inbox** (`platform.outbox`, `platform.inbox`,
   `platform.consumer_checkpoint`).  There is no shared queue table.
6. **Append-only where the domain says immutable** (signals, decisions, publications,
   deliveries, snapshots): enforced by permissions (no `UPDATE/DELETE` grant) plus
   triggers, not by convention.

## 2. `trading` database

| Schema | Owning context | Tables (target) | Notes |
|---|---|---|---|
| `catalog` | Market | `instrument`, `instrument_provider_symbol`, `provider` | replaces `symbol_mappings` in `platform.json` |
| `strategy` | Strategy | `strategy`, `strategy_version`, `parameter_set`, `freeze_manifest`, `signal_stream`, `stream_binding`, `stream_state_history`, `runner_state`, strategy-internal ledgers (`context_setup`, `context_entry_opportunity`, `liquidity_setup` …) | today's `strategy.phase6_*`, `strategy.runner_state`, `platform.{strategy,configuration}_versions`, `platform.freeze_manifests` |
| `signals` | Signals | `entry_signal`, `publication_decision`, `published_signal`, `signal_retraction`, `publication_policy`, `startup_watermark` | replaces `orchestration.signals` stub, `startup_epoch.json`, `signals.jsonl` |
| `trade_management` | Trade Management | `tm_version`, `managed_trade`, `managed_trade_state_history`, `observation`, `decision`, `management_publication`, `checkpoint`, `counterfactual_*` | today's `trade_management.{observations,decisions,checkpoints}` |
| `performance` | Performance | `closed_trade`, `performance_observation`, `series_definition`, `snapshot`, `evidence_bundle`, `research_run`, `methodology_version`, `cost_model_version` | new; seeded from the results registry |
| `execution` | Personal Execution | `route_policy`, `sizing_decision`, `execution_intent`, `broker_attempt`, `order`, `fill`, `reconciliation`, `ownership`, `broker_state`, `management_translation`, `kill_switch`, `resume_generation`, `lease` | **restricted role**; today's `execution.*` stubs + `ownership_registry.jsonl`, `broker_state.json`, `management_*.jsonl` |
| `platform` | kernel | `outbox`, `inbox`, `consumer_checkpoint`, `schema_migrations`, `system_metadata`, `lease` | generic |
| `audit`, `telemetry` | kernel | `audit_event`, `safety_invariant`, `unattached_event`, runtime health | today's `audit.*`, `telemetry.*` |

Ownership of *state transitions*: only the owning context's code writes its schema;
other contexts read through events or read-only views.  In particular the **Publication
Gate is the only writer of `signals.published_signal`**, and **Personal Execution never
writes `signals`, `trade_management` or `performance`** (it emits `live.trade.closed`).

## 3. `commerce` database

| Schema | Owning context | Tables (target) |
|---|---|---|
| `identity` | Customers | `customer`, `user_account`, `idp_link`, `consent`, `terms_acceptance` |
| `catalog` | Catalog | `stream_listing` (holds `stream_id` by value), `strategy_listing`, `instrument_display`, `channel_type`, `premium_capability`, `listing_visibility_history` |
| `pricing` | Pricing | `price_book` (versioned), `pricing_component`, `pricing_rule`, `price_quote` |
| `subscription` | Subscriptions | `subscription`, `subscription_item`, `entitlement`, `subscription_status_history` |
| `billing` | Billing boundary | `billing_customer_ref`, `provider_event` (raw webhooks), `invoice_ref`, `payment_ref` — **provider is the system of record**; no card data |
| `distribution` | Distribution | `channel_endpoint` (verified), `notification_preference`, `message_template`, `message`, `signal_delivery`, `delivery_attempt`, `suppression` (incl. `SuppressedByEntitlement`) |
| `readmodel` | Read models | `published_signal_view`, `management_signal_view`, `signal_outcome_view`, `active_trade_view`, `stream_public_view`, `performance_publication` |
| `content` | Content | `testimonial`, `testimonial_moderation` |
| `platform`, `audit` | kernel | `outbox`, `inbox`, `consumer_checkpoint`, `audit_event` |

## 4. References that cross the boundary (by value only)

| Commerce field | Refers to (trading) | Integrity mechanism |
|---|---|---|
| `catalog.stream_listing.stream_id` | `strategy.signal_stream.stream_id` | `stream.catalog.updated` events; nightly reconciliation report |
| `readmodel.*.signal_id` | `signals.published_signal.signal_id` | events carry the id; view rows are immutable |
| `readmodel.*.managed_trade_id` | `trade_management.managed_trade.managed_trade_id` | events |
| `readmodel.performance_publication.snapshot_id` | `performance.snapshot.snapshot_id` | `performance.snapshot.computed` + `evidence_hash` stored with the publication |

Nothing in `trading` refers to a commerce id.

## 5. Mapping of the existing PostgreSQL work (Codex reconciliation)

The current migrations (`postgres/migrations/001–007`, working-set importer) are a
sound **offline foundation for the Context strategy's Phase-6 state** and are reusable:
versioned checksummed migrations, stable-id idempotent upserts, append-only lifecycle
rows, single mutable owner per entity, explicit import boundaries.  What changes is
*schema placement*, and it is cheap **now** because no production writer exists yet
(verified: nothing outside `postgres/` imports the package; no trading schema exists in
the live server).

| Existing | Target | Action |
|---|---|---|
| `platform.strategy_versions`, `configuration_versions`, `freeze_manifests` | `strategy.strategy_version`, `parameter_set`/config, `freeze_manifest` | move schema before first live write |
| `strategy.phase6_*`, `strategy.runner_state`, `strategy.economic_positions` | `strategy.context_*` (strategy-internal ledger) | keep; rename `phase6_` prefix to the strategy key |
| `platform.migration_boundaries`, `import_batches`, `migration_batches`, `research.phase6_observations`, `telemetry.phase6_unattached_events` | `migration.*` / `research.*` | keep as migration tooling, out of the runtime schemas |
| `orchestration.signals` (stub, migration 003) | `signals.entry_signal` | **rename/reshape**; add `publication_decision`, `published_signal` |
| `execution.execution_intents/broker_attempts/orders/fills/reconciliation` (stubs) | `execution.*` | keep in the restricted schema |
| `trade_management.observations/decisions/checkpoints` (stubs) | `trade_management.*` | keep; add `managed_trade`, `tm_version` |
| schema `orchestration` | retired | its content splits into `signals` and `execution` |
| *(missing)* | `platform.outbox/inbox/consumer_checkpoint`, `execution.lease` | **add** — these are the actual gaps found in the audit (no outbox, inbox, fencing) |

Recommendation for Codex: before writing migration 008+, adopt the schema names above;
treat `orchestration.*` as deprecated.  No data migration is needed because the tables
are empty in every environment.

## 6. Infrastructure implications (from the live-cluster audit)

* The existing Postgres, NATS and Redis in the cluster's `default` namespace are
  **shared with another product** (eleven service databases; 14 NATS clients; none from
  trading).  StratRelay's `trading` and `commerce` databases must not live in that
  instance: different blast radius, different retention/backup, and a customer-data
  system must not share a NATS account with unrelated services.  **Recommendation:**
  dedicated namespace, dedicated Postgres cluster (two databases) and dedicated NATS
  with JetStream, separate NATS accounts for `trading`, `execution-private`, `commerce`.
  (Open decision **O9**.)
* The current Postgres PVC uses `reclaimPolicy: Delete` on `local-path`; a production
  datastore needs a retained/replicated volume and tested backup/restore.
* NATS has no persistence today; JetStream needs a PVC and `-js` — design decision only.

## 7. JSONL / Parquet policy

| Format | Allowed role |
|---|---|
| JSONL | research exports, audit exports, backtest inputs; **transitional** production IPC until each stream in `04_…` §5 is retired |
| Parquet | research datasets and evidence bundles, referenced from `performance.evidence_bundle` by content hash (object storage), never the record of truth |
| Files under `runtime/` | must shrink to process artefacts (pid, logs); state moves to PostgreSQL |

The 14 mutable strategy state/manifest/summary files tracked at the repository root
(A1 finding S5) are runtime state committed as source and belong in `strategy.*` /
`performance.*` tables (or ignored artefacts) before Stage 2.
