# 15 - Observability requirements

Each requirement exists to make a **gate** (`12`) measurable or a **failure mode** (`10`) detectable. Metric names are proposals.

## 1. Metrics

| Area | Metric | Feeds |
|---|---|---|
| Outbox | `outbox_unpublished_count`, `outbox_oldest_unpublished_age_seconds`, `outbox_publish_attempts_total{result}`, `outbox_table_bytes` | DUAL_WRITE_RECONCILED, failure #2/#5/#15 |
| Publisher | `jetstream_publish_ack_latency_seconds`, `jetstream_publish_errors_total` | JETSTREAM_SHADOW_READY |
| Consumer | `consumer_lag_messages`, `consumer_lag_seconds`, `consumer_ack_pending`, `consumer_redeliveries_total`, `consumer_parked_total`, `handler_duration_seconds` | JETSTREAM_* gates, #10/#11 |
| Inbox | `inbox_duplicate_hits_total{consumer}`, `inbox_table_bytes` | #3/#7/#9 |
| Reconciliation | `reconciliation_findings{class,domain}`, `reconciliation_last_clean_timestamp{domain}`, `reconciliation_run_duration_seconds` | all gates |
| Authority/fencing | `authority_mode{domain}`, `lease_generation{resource}`, `lease_remaining_seconds{resource}`, `lease_lost_total`, `fence_rejections_total` | cutover, split-brain detection |
| Execution | `intent_state_total{state}`, `intent_age_at_send_seconds`, `attempts_uncertain{account}`, `uncertain_oldest_age_seconds`, `expired_intents_total{reason}`, `duplicate_delivery_suppressed_total` | execution gates, alarms |
| Broker state | `broker_snapshot_age_seconds{account}`, `broker_snapshot_version{account}`, `broker_refresh_errors_total` (the consumer currently swallows these) | authorisation freshness |
| Signals | `signal_ingest_lag_seconds{stream}`, `signals_stale_on_discovery_total`, `replay_watermark_age` | signal gates |
| Projector | `projector_lag_seconds{domain}`, `projector_fidelity_mismatch_total` | mirror phase |
| File-IPC | `legacy_file_reads_total{path}`, `legacy_file_writes_total{path}` (from an access audit, not app code) | LEGACY_*_RETIRE_READY |
| Runtime | `runtime_instance_info{modes,build}`, `heartbeat_age_seconds{service}`, `db_up`, `nats_up` | modes `14` |

## 2. Alerts (severity)

| Alert | Condition | Sev |
|---|---|---|
| Execution halted account | any `attempts_uncertain > 0` older than 5 min | page |
| Lease lost while acting / fence rejection | `lease_lost_total` or `fence_rejections_total` increase | page |
| DB unreachable in `DB_PRIMARY` | `db_up == 0` > 15 s | page |
| Outbox stuck | oldest unpublished > 30 s (> 10 s for execution/management) | page / warn |
| Consumer lag | > 5 s (execution) / > 30 s (others) | warn |
| Reconciliation blocking finding | any in a domain at or beyond `DUAL_WRITE_RECONCILED` | page for authority domains |
| Split brain | two holders observed for one resource, or file/DB authority both true | page |
| Broker snapshot stale | > 90 s (before the 120 s fail-closed threshold) | warn |
| Poison message parked | `consumer_parked_total` increase | warn |
| Legacy file touched in `LEGACY_PROJECTION=off` | any read/write by a service | page |

## 3. Logs and traces

Structured JSON with `runtime_instance_id`, `event_id`, `aggregate`, `correlation_id`, `causation_id`, `generation`; the correlation id follows `signal_id` → `execution_intent_id` → `attempt` → broker ticket. **No secrets, no connection strings, no full trace payloads.** A single "explain" query given a `signal_id` must return: signal, evaluation reference, route/sizing, intent, attempts, results, management actions, events emitted/received.

## 4. Dashboards (minimum)

1. **Migration board** per domain: current mode, gate status, last clean reconciliation, open findings by class.
2. **Pipeline health**: outbox → JetStream → consumer lag, redelivery, parked.
3. **Execution safety**: intents by state, freshness at send, uncertain attempts, lease/generation, broker snapshot age.
4. **Authority**: `authority_mode`, generation history, arming audit.
5. **File-IPC residue**: which paths are still read/written and by whom (drives the retire gates).

## 5. Audit trail

State-changing operator actions (arm/disarm, activation change, generation bump, cutover, rollback, manual reconciliation resolution) are rows in an `audit_event` table with actor, reason, before/after hashes - the migration is itself an auditable sequence.

## 6. Retention

Operational logs 30 d; reconciliation runs and findings 1 y; outbox published rows 7-14 d then archived; inbox ≥ max redelivery horizon + margin; audit_event indefinitely (subject to policy). Sizing is an open decision (OD-08).
