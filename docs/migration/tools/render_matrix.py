#!/usr/bin/env python3
"""Single source of truth for the artifact-by-artifact file-IPC inventory and migration matrix.

    python3 docs/migration/tools/render_matrix.py            # regenerate data + generated docs
    python3 docs/migration/tools/render_matrix.py --check    # exit 1 if a rule below is violated

Outputs (all generated - edit THIS file, never the outputs):
    docs/migration/data/migration_matrix.csv
    docs/migration/01_FILE_IPC_INVENTORY.md
    docs/migration/04_ARTIFACT_MIGRATION_MATRIX.md

Read-only: writes only under docs/migration/.  Records are derived from reading the source
at the baseline commit; paths are relative to TRADING_PLATFORM_RUNTIME_DIR unless noted.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, OrderedDict
from pathlib import Path

DOCS = Path.cwd() / "docs" / "migration"

CATEGORIES = ["AUTHORITATIVE_STATE", "EVENT_TRANSPORT", "REBUILDABLE_PROJECTION", "CHECKPOINT",
              "CONFIGURATION", "RESEARCH_ARTIFACT", "DEBUG_ARTIFACT"]
ROLLBACK = ["REVERSIBLE", "CONDITIONALLY_REVERSIBLE", "ONE_WAY_WITH_MIGRATION", "NOT_APPLICABLE"]
# Migration strategies (defined in 03/04): not a mechanical ladder - each artifact picks its own.
STRATEGIES = {
    "OUTBOX_PROJECTOR": "domain tx + outbox; compatibility projector writes the legacy file from committed state (no app dual-write)",
    "TAILER_INGEST": "legacy writer unchanged (frozen); an idempotent tailer ingests its file into PostgreSQL and emits events",
    "DIRECT_CUTOVER": "single authority instant under an explicit fence; no long-lived dual authority",
    "DB_ONLY": "state has no file consumer worth mirroring; becomes a PostgreSQL row (+ event) directly",
    "REPLACE_BY_EVENTS": "file is a transport; replaced by JetStream subject + durable consumer + inbox",
    "REBUILD": "projection is regenerated from PostgreSQL/events; nothing is migrated",
    "KEEP": "stays a file (config / research / debug); explicitly out of PRODUCTION_FILE_IPC scope",
    "RETIRE": "no live consumer (or dead code); delete after a proof-of-no-reader window",
}

# fields: id, path, cat, writers, readers, update, durability, ordering, ack, recovery, dup, fresh,
#         authority, target, strategy, risk, rollback, debt, phase, notes
A: list[dict] = []


def add(**kw):
    kw.setdefault("debt", "")
    kw.setdefault("notes", "")
    A.append(kw)


# ---------------------------------------------------------------- orchestration
add(id="ORC-01", path="orchestration/signals.jsonl", cat="AUTHORITATIVE_STATE",
    writers="signal_orchestrator.poll_once via OrchestrationStore.append (unique_key=signal_id)",
    readers="live_execution_consumer.source_records; control_api; orchestrator report/CLI",
    update="append-only; whole-file scan for unique_key before append (O(n))",
    durability="flush only, no fsync; single writer process assumed",
    ordering="file order = discovery order; no sequence number",
    ack="none - consumer polls whole file every ~1s and derives 'new' by absence of an intent",
    recovery="re-scan; replay guard (startup_epoch) filters discovery; a torn last line breaks rows() (no tolerance)",
    dup="unique_key=signal_id (stable_id of identity) prevents duplicate rows",
    fresh="signal_emitted_at = orchestrator discovery time, not strategy detection time",
    authority="AUTHORITATIVE for 'signal exists'; also used as a queue",
    target="signals table (PK signal_id, canonical_hash) + outbox event signal.entry.created",
    strategy="TAILER_INGEST", risk="MEDIUM", rollback="CONDITIONALLY_REVERSIBLE",
    debt="MULTI-ROLE: authoritative record AND consumer queue", phase="P1-P4")
add(id="ORC-02", path="orchestration/route_decisions.jsonl", cat="AUTHORITATIVE_STATE",
    writers="signal_orchestrator.route_signal", readers="control_api; report/CLI (no service consumer found)",
    update="append-only, unique_key", durability="flush only", ordering="file order",
    ack="none", recovery="regenerated on re-route only if signal not marked processed",
    dup="unique_key", fresh="decision time", authority="AUTHORITATIVE routing decision record (audit)",
    target="route_decisions table keyed (signal_id, stream_id)", strategy="OUTBOX_PROJECTOR", risk="LOW",
    rollback="REVERSIBLE", phase="P2")
add(id="ORC-03", path="orchestration/sizing_decisions.jsonl", cat="AUTHORITATIVE_STATE",
    writers="signal_orchestrator.route_signal",
    readers="live_execution_consumer.create_intents (input to execution intent); control_api",
    update="append-only, unique_key", durability="flush only", ordering="file order",
    ack="none - consumer re-derives which decisions lack an intent",
    recovery="rescan", dup="unique_key", fresh="sizing computed against an account snapshot at routing time",
    authority="AUTHORITATIVE sizing record; also an input queue for execution",
    target="sizing_decisions table + event sizing.decision.recorded (execution consumes the event, not the row set)",
    strategy="OUTBOX_PROJECTOR", risk="HIGH", rollback="CONDITIONALLY_REVERSIBLE",
    debt="MULTI-ROLE: decision record AND execution input queue", phase="P3")
add(id="ORC-04", path="orchestration/tradeability_decisions.jsonl", cat="AUTHORITATIVE_STATE",
    writers="signal_orchestrator.route_signal (line ~214)", readers="control_api",
    update="append-only, unique_key",
    durability="unknown", ordering="-", ack="none", recovery="-", dup="-", fresh="-",
    authority="UNVERIFIED - stream is not declared in OrchestrationStore.paths, so the append raises KeyError (verified by reading; same in the original repo HEAD)",
    target="tradeability_decisions table (if the path is proven live)", strategy="DB_ONLY", risk="MEDIUM",
    rollback="REVERSIBLE", debt="LATENT DEFECT: undeclared stream; runtime behaviour unknown", phase="P0-verify",
    notes="Open decision OD-01: verify against a running system whether route_signal degrades (ROUTE_DEGRADED) here.")
add(id="ORC-05", path="orchestration/account_snapshots.jsonl", cat="REBUILDABLE_PROJECTION",
    writers="signal_orchestrator.route_signal", readers="control_api; sizing evidence",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="-",
    dup="unique_key", fresh="snapshot time", authority="none - derived from broker state at routing time",
    target="account_snapshot rows referenced by sizing_decisions (evidence, not authority)", strategy="REBUILD",
    risk="LOW", rollback="REVERSIBLE", phase="P2")
add(id="ORC-06", path="orchestration/classification_corrections.jsonl", cat="AUTHORITATIVE_STATE",
    writers="signal_orchestrator.poll_once", readers="signal_orchestrator; live_execution_consumer.source_records",
    update="append-only", durability="flush only", ordering="file order", ack="none",
    recovery="rescan", dup="unique key", fresh="-", authority="AUTHORITATIVE operator/system correction that changes routing/execution",
    target="signal_classification_corrections table + event", strategy="TAILER_INGEST", risk="MEDIUM",
    rollback="CONDITIONALLY_REVERSIBLE", phase="P2")
add(id="ORC-07", path="orchestration/events.jsonl", cat="DEBUG_ARTIFACT",
    writers="OrchestrationStore.append('events')", readers="control_api; report", update="append-only",
    durability="flush only", ordering="file order", ack="none", recovery="-", dup="none guaranteed",
    fresh="-", authority="none", target="operational logs + audit_event table for state-changing entries",
    strategy="REBUILD", risk="LOW", rollback="REVERSIBLE", phase="P2")
add(id="ORC-08", path="orchestration/distribution_queue.jsonl", cat="EVENT_TRANSPORT",
    writers="signal_orchestrator.route_signal", readers="control_api and report only; NO service consumer",
    update="append-only", durability="flush only", ordering="file order", ack="none - nobody consumes",
    recovery="-", dup="-", fresh="-", authority="none",
    target="none now; future delivery uses subscription.delivery.* events", strategy="RETIRE", risk="LOW",
    rollback="REVERSIBLE", debt="PLACEHOLDER transport with no consumer", phase="P6")
add(id="ORC-09", path="orchestration/delivery_status.jsonl", cat="REBUILDABLE_PROJECTION",
    writers="signal_orchestrator (DELIVERY_ATTEMPTED/COMPLETE/ROUTE_DEGRADED)", readers="control_api; report only",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="-", dup="-", fresh="-",
    authority="none", target="derived from signal/route/outbox state", strategy="RETIRE", risk="LOW",
    rollback="REVERSIBLE", debt="placeholder projection", phase="P6")
add(id="ORC-10", path="orchestration/state.json", cat="CHECKPOINT",
    writers="OrchestrationStore.save_state (tmp + os.replace, no fsync)",
    readers="signal_orchestrator.poll_once (processed_signal_ids)", update="whole-file replace",
    durability="atomic rename, no fsync", ordering="n/a", ack="IS the ack of signal processing",
    recovery="loss => reprocessing guarded only by signals.jsonl unique_key and replay guard",
    dup="processed_signal_ids grows unbounded", fresh="-", authority="CHECKPOINT for the orchestrator, but it also is the only 'processed' marker (correctness depends on it)",
    target="orchestrator_checkpoint row + inbox/outbox; correctness moves to constraints", strategy="DB_ONLY",
    risk="MEDIUM", rollback="CONDITIONALLY_REVERSIBLE", debt="MULTI-ROLE: checkpoint AND correctness dedupe set", phase="P2")
add(id="ORC-11", path="orchestration/startup_epoch.json (replay guard)", cat="AUTHORITATIVE_STATE",
    writers="orchestration.replay_guard", readers="orchestration adapters (filter discovery by watermark)",
    update="written at startup", durability="unknown (helper)", ordering="n/a", ack="n/a",
    recovery="missing => guard recreated; could re-admit historic signals", dup="n/a",
    fresh="watermark", authority="AUTHORITATIVE: decides which historic signals are ignored (a safety boundary)",
    target="replay_watermark row per (stream, environment) with generation", strategy="DIRECT_CUTOVER", risk="HIGH",
    rollback="CONDITIONALLY_REVERSIBLE", phase="P2")
add(id="ORC-12", path="orchestration/<instance>-startup-baseline.json", cat="CHECKPOINT",
    writers="liquidity instance publisher / orchestrator", readers="LiquidityInstanceAdapter",
    update="replace", durability="unknown", ordering="n/a", ack="n/a", recovery="regenerate: risks re-emitting old signals",
    dup="-", fresh="-", authority="CHECKPOINT (which liquidity signals pre-existed)",
    target="stream_baseline row per stream instance", strategy="DB_ONLY", risk="MEDIUM",
    rollback="CONDITIONALLY_REVERSIBLE", phase="P2")
add(id="ORC-13", path="orchestration/{manifest,heartbeat,orchestrator.pid,management_health}.json + /tmp/signal-orchestrator-{shadow,real}.stop",
    cat="DEBUG_ARTIFACT", writers="signal_orchestrator.run / acquire_lock",
    readers="control_api; operators; scripts (PID + stop file = process control)",
    update="replace / create / touch", durability="no fsync", ordering="n/a", ack="n/a",
    recovery="PID check-then-write (TOCTOU, no lease/fencing token)", dup="two instances possible under race",
    fresh="heartbeat timestamp", authority="none for data; PID file is de-facto process singleton",
    target="runtime_instance + heartbeat rows (lease); stop = control command in DB or signal", strategy="DB_ONLY",
    risk="MEDIUM", rollback="REVERSIBLE", debt="PID file acts as singleton lock without fencing", phase="P1")

# ---------------------------------------------------------------- execution
add(id="EXE-01", path="execution/execution_intents.jsonl", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer.create_intents (ExecutionStore.append)",
    readers="live_execution_consumer.process_intents (same process); control_api",
    update="append-only", durability="flush only",
    ordering="file order", ack="none - intent 'done' == a decision row exists for stable_id('DEC',{intent})",
    recovery="restart re-derives pending intents by absence of a decision row: crash between broker send and decision append => duplicate-risk window (mitigated only by idempotency key + bridge)",
    dup="intent id stable; idempotency key stable_id('REALORDER',{execution_intent_id, account_context_id})",
    fresh="intent_max_age 5 s, signal_max_age 120 s enforced at process time",
    authority="AUTHORITATIVE (intent existence) AND transport to the sender",
    target="execution_intent table (PK execution_intent_id, unique idempotency_key, state machine) + event execution.intent.created",
    strategy="DIRECT_CUTOVER", risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION",
    debt="MULTI-ROLE: intent record AND queue AND source of 'sent?' truth", phase="P5")
add(id="EXE-02", path="execution/execution_decisions.jsonl", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer.process_intents (appended AFTER broker send)",
    readers="live_execution_consumer (completion test); control_api",
    update="append-only", durability="flush only", ordering="file order", ack="its presence is the ack of the intent",
    recovery="missing after a crash => intent replayed", dup="decision id stable_id('DEC',{intent})",
    fresh="-", authority="AUTHORITATIVE broker-send outcome",
    target="execution_attempt rows written BEFORE send (SUBMISSION_ATTEMPTED) and completed after; uncertain ack representable",
    strategy="DIRECT_CUTOVER", risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION",
    debt="no durable pre-send record (smoke path already has SUBMISSION_ATTEMPTED - precedent)", phase="P5")
add(id="EXE-03", path="execution/events.jsonl", cat="DEBUG_ARTIFACT", writers="ExecutionStore", readers="control_api",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="-", dup="-", fresh="-",
    authority="none", target="audit_event + logs", strategy="REBUILD", risk="LOW", rollback="REVERSIBLE", phase="P5")
add(id="EXE-04", path="execution/account_snapshots.jsonl + market_snapshots.jsonl", cat="REBUILDABLE_PROJECTION",
    writers="live_execution_consumer (broker refresh)", readers="control_api; evidence for sizing/execution",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="-", dup="-",
    fresh="per refresh", authority="none - broker is the truth",
    target="evidence rows referenced by attempts; NO tick history in PostgreSQL", strategy="REBUILD", risk="LOW",
    rollback="REVERSIBLE", phase="P5")
add(id="EXE-05", path="execution/execution_skips.jsonl", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer (expiry/duplicate/guard skips)", readers="control_api",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="-", dup="-", fresh="-",
    authority="AUTHORITATIVE reason an intent was not sent (auditable)", target="execution_intent terminal state SKIPPED(reason_code)",
    strategy="DIRECT_CUTOVER", risk="MEDIUM", rollback="ONE_WAY_WITH_MIGRATION", phase="P5")
add(id="EXE-06", path="execution/state.json", cat="CHECKPOINT", writers="ExecutionStore.save_state (tmp + replace)",
    readers="live_execution_consumer", update="replace", durability="atomic rename, no fsync", ordering="n/a",
    ack="-", recovery="loss must not change correctness (derived from decisions)", dup="-", fresh="-",
    authority="CHECKPOINT", target="none needed once correctness is transactional", strategy="RETIRE", risk="LOW",
    rollback="REVERSIBLE", phase="P5")
add(id="EXE-07", path="execution/real_state.json", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer.arm_real", readers="live_execution_consumer; control_api",
    update="replace", durability="atomic rename", ordering="n/a", ack="n/a",
    recovery="operator re-arm", dup="-", fresh="armed_at", authority="AUTHORITATIVE: REAL execution armed/disarmed",
    target="execution_authority row (armed, generation) - PART OF THE FENCE, see 08", strategy="DIRECT_CUTOVER",
    risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION", phase="P5")
add(id="EXE-08", path="execution/real_execution_resume.json", cat="AUTHORITATIVE_STATE",
    writers="establish_execution_resume_cutoff (tmp + replace)", readers="live_execution_consumer.source_records filter",
    update="replace; generation+1, cutoff timestamp, excluded_signal_ids", durability="atomic rename",
    ordering="generation is operator epoch, NOT a per-write fencing token", ack="n/a",
    recovery="the only barrier that stops old signals from executing after an outage", dup="-",
    fresh="cutoff", authority="AUTHORITATIVE execution-authority epoch (resume generation)",
    target="execution_authority_generation table (monotonic, DB-enforced) referenced by every intent", strategy="DIRECT_CUTOVER",
    risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION",
    debt="generation semantics are the migration's fence; must never be re-readable from a stale file after cutover", phase="P5")
add(id="EXE-09", path="execution/real_execution_resume_generations/ (referenced by no source file)", cat="AUTHORITATIVE_STATE",
    writers="UNKNOWN - no reference in source at baseline", readers="UNKNOWN", update="unknown", durability="unknown",
    ordering="-", ack="-", recovery="-", dup="-", fresh="-", authority="UNVERIFIED (possible historic generation archive)",
    target="import as historical generations if it exists in the live runtime dir", strategy="DIRECT_CUTOVER",
    risk="MEDIUM", rollback="NOT_APPLICABLE", debt="orphan-looking directory", phase="P0-verify",
    notes="Open decision OD-02: inspect live runtime dir (read-only) before P5.")
add(id="EXE-10", path="execution/real_trades.jsonl", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer (real fills/positions record)", readers="control_api; management",
    update="append-only", durability="flush only", ordering="file order", ack="none", recovery="reconcile with broker_state",
    dup="-", fresh="-", authority="AUTHORITATIVE record of what this platform believes it opened (broker is the ultimate truth)",
    target="broker_order / fill rows + event execution.fill.observed", strategy="DIRECT_CUTOVER", risk="HIGH",
    rollback="ONE_WAY_WITH_MIGRATION", phase="P5")
add(id="EXE-11", path="execution/real_smoke/{smoke_state.json,events.jsonl}", cat="DEBUG_ARTIFACT",
    writers="smoke path (writes SUBMISSION_ATTEMPTED before send)", readers="operators", update="append/replace",
    durability="flush only", ordering="file order", ack="-", recovery="-", dup="-", fresh="-", authority="none",
    target="smoke tests run against DB in a dedicated environment", strategy="KEEP", risk="LOW",
    rollback="REVERSIBLE", phase="P6", notes="Keep for smoke; must not be required in production.")
add(id="EXE-12", path="execution/management/execution_results.jsonl", cat="AUTHORITATIVE_STATE",
    writers="live_execution_consumer.process_management_intents", readers="live_execution_consumer (completed set); control_api",
    update="append-only", durability="flush only", ordering="file order",
    ack="only COMPLETED counts as done - failed intents are retried",
    recovery="retry until COMPLETED", dup="stable ids", fresh="-", authority="AUTHORITATIVE result of a management action",
    target="management_execution_result rows + event management.execution.result", strategy="DIRECT_CUTOVER",
    risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION", phase="P5")
add(id="EXE-13", path="execution/demo_{state.json,events.jsonl,trades.jsonl}", cat="DEBUG_ARTIFACT",
    writers="execution/demo.py + consumer demo mode", readers="control_api", update="mixed", durability="flush only",
    ordering="-", ack="-", recovery="-", dup="-", fresh="-", authority="none", target="retire or run against DB in demo environment",
    strategy="RETIRE", risk="LOW", rollback="REVERSIBLE", phase="P6", notes="Open decision OD-04: is demo still used?")
add(id="EXE-14", path="execution/{heartbeat.json,execution.pid}", cat="DEBUG_ARTIFACT",
    writers="live_execution_consumer", readers="control_api; scripts", update="replace/create", durability="no fsync",
    ordering="n/a", ack="n/a", recovery="PID check-then-write", dup="two consumers possible under race",
    fresh="heartbeat timestamp", authority="PID = de-facto singleton lock",
    target="runtime_instance lease + fencing token (see 08)", strategy="DIRECT_CUTOVER", risk="CRITICAL",
    rollback="ONE_WAY_WITH_MIGRATION", debt="singleton by PID file without fencing", phase="P1/P5")
add(id="EXE-15", path="<bridge repo>/runtime/execution_bridge/request_lifecycle.jsonl (cwd-relative read, ~line 1804)",
    cat="EVENT_TRANSPORT", writers="MT5 bridge (other repo)", readers="live_execution_consumer",
    update="append-only in the bridge", durability="bridge-defined", ordering="bridge file order",
    ack="none", recovery="polled", dup="-", fresh="-", authority="none - bridge reports what MT5 said",
    target="bridge lifecycle reported through the MT5 bridge client boundary (API/event), not a shared file",
    strategy="REPLACE_BY_EVENTS", risk="HIGH", rollback="CONDITIONALLY_REVERSIBLE",
    debt="cross-repository file coupling; breaks the extraction boundary", phase="P5")

# ---------------------------------------------------------------- management / broker state
add(id="MGT-01", path="management/ownership_registry.jsonl", cat="AUTHORITATIVE_STATE",
    writers="trade_manager.central.OwnershipRegistry (append_unique: scan-then-append, not atomic)",
    readers="central.authorize (prove()); trade manager", update="append-only provenance ledger",
    durability="flush only", ordering="file order", ack="none", recovery="rows() tolerant of bad lines",
    dup="append_unique scans for a key - race possible with two writers", fresh="-",
    authority="AUTHORITATIVE: which position belongs to which signal/strategy/manager (provenance)",
    target="position_ownership table (PK position_id, source, generation) - domain data; this is NOT the runtime-fence",
    strategy="OUTBOX_PROJECTOR", risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION", phase="P4")
add(id="MGT-02", path="management/broker_state.json", cat="AUTHORITATIVE_STATE",
    writers="BrokerStateStream.save / refresh_from_provider (tmp + replace; snapshot_version)",
    readers="central.authorize (snapshot-version match, stale check 120 s); trade manager; control_api",
    update="whole-file replace with monotonic snapshot_version", durability="atomic rename, no fsync",
    ordering="snapshot_version", ack="version match at authorization time",
    recovery="a stale or missing snapshot fails authorization closed (healthy(120s))", dup="version",
    fresh="120 s", authority="AUTHORITATIVE cache of broker truth for authorization decisions; the broker remains the ultimate truth",
    target="broker_state_snapshot (aggregate version, as_of, positions/orders/account, hash) - latest + bounded history, NO ticks",
    strategy="OUTBOX_PROJECTOR", risk="HIGH", rollback="CONDITIONALLY_REVERSIBLE",
    debt="MULTI-ROLE: state snapshot AND change notification (readers poll it)", phase="P3-P4")
add(id="MGT-03", path="management/management_proposals.jsonl", cat="AUTHORITATIVE_STATE",
    writers="stream_consumer (REAL_MANAGEMENT) via append_unique", readers="central.authorize_pending_proposals",
    update="append-only", durability="flush only", ordering="file order", ack="presence of a decision",
    recovery="rows() tolerant", dup="append_unique key (action key)", fresh="-",
    authority="AUTHORITATIVE proposal record AND queue to the authorizer",
    target="management_proposal table + event management.proposal.created", strategy="OUTBOX_PROJECTOR", risk="HIGH",
    rollback="CONDITIONALLY_REVERSIBLE", debt="MULTI-ROLE: record + queue", phase="P4")
add(id="MGT-04", path="management/management_intents.jsonl", cat="AUTHORITATIVE_STATE",
    writers="central.authorize_pending_proposals", readers="live_execution_consumer.process_management_intents",
    update="append-only", durability="flush only", ordering="file order",
    ack="COMPLETED result row in execution/management/execution_results.jsonl",
    recovery="non-COMPLETED intents retried", dup="duplicate action key rejected at authorization", fresh="-",
    authority="AUTHORITATIVE authorized action AND queue to executor",
    target="management_intent table + event management.intent.authorized; executor idempotent by intent id",
    strategy="DIRECT_CUTOVER", risk="CRITICAL", rollback="ONE_WAY_WITH_MIGRATION",
    debt="MULTI-ROLE: authorization record + execution queue", phase="P5")
add(id="MGT-05", path="management/management_decisions.jsonl", cat="AUTHORITATIVE_STATE",
    writers="central.authorize (accept/reject with reasons)", readers="control_api; audit", update="append-only",
    durability="flush only", ordering="file order", ack="-", recovery="-", dup="key", fresh="-",
    authority="AUTHORITATIVE authorization decision (audit)", target="management_decision table", strategy="OUTBOX_PROJECTOR",
    risk="MEDIUM", rollback="CONDITIONALLY_REVERSIBLE", phase="P4")

# ---------------------------------------------------------------- trade manager observation fanout
add(id="TMG-01", path="trade_manager/observation_stream/events.jsonl (cwd-relative default; rotates at 50 MB)",
    cat="EVENT_TRANSPORT", writers="fanout.SharedObservationPublisher (sequence numbers)",
    readers="fanout.FanoutConsumer (stream_consumer / phase 7 observer)", update="append; rotate at 50 MB",
    durability="flush only", ordering="publisher-assigned sequence; gap detection on read", ack="consumer checkpoint",
    recovery="gap => detected, not repaired", dup="published_ids set", fresh="-",
    authority="none - observations", target="JetStream stream OBSERVATION (bounded retention); high volume stays OFF operational subjects",
    strategy="REPLACE_BY_EVENTS", risk="MEDIUM", rollback="REVERSIBLE", phase="P4")
add(id="TMG-02", path="trade_manager/observation_stream/publisher_state.json", cat="CHECKPOINT",
    writers="SharedObservationPublisher", readers="SharedObservationPublisher", update="replace",
    durability="unknown", ordering="-", ack="-", recovery="stores ALL published_ids: unbounded growth",
    dup="dedupe set", fresh="-", authority="CHECKPOINT+dedupe (correctness relevant)",
    target="none: JetStream Nats-Msg-Id dedupe window + event_id inbox", strategy="RETIRE", risk="LOW",
    rollback="REVERSIBLE", debt="unbounded state", phase="P4")
add(id="TMG-03", path="trade_manager/observation_stream/<consumer>_checkpoint*.json", cat="CHECKPOINT",
    writers="FanoutConsumer.read persists checkpoint BEFORE processing", readers="FanoutConsumer",
    update="replace", durability="unknown", ordering="sequence", ack="checkpoint == ack (at-most-once)",
    recovery="crash after checkpoint and before processing LOSES observations", dup="-", fresh="-",
    authority="CHECKPOINT", target="JetStream durable consumer (ack after processing) + inbox",
    strategy="REPLACE_BY_EVENTS", risk="MEDIUM", rollback="REVERSIBLE",
    debt="at-most-once semantic bug class", phase="P4")
add(id="TMG-04", path="trade_manager/{collector_state.json,collector_health.json}", cat="CHECKPOINT",
    writers="SharedStreamTradeManager", readers="SharedStreamTradeManager; control_api",
    update="replace", durability="unknown", ordering="-", ack="-",
    recovery="positions are in-memory only: after restart they are rebuilt from the stream/broker state",
    dup="-", fresh="health", authority="CHECKPOINT/health", target="trade_manager_checkpoint row + heartbeat",
    strategy="DB_ONLY", risk="MEDIUM", rollback="CONDITIONALLY_REVERSIBLE", phase="P4")
add(id="TMG-05", path="trade_manager/activation.json (cwd-relative default)", cat="AUTHORITATIVE_STATE",
    writers="operator", readers="stream_consumer (activation mode: shadow/real management)",
    update="manual edit", durability="-", ordering="-", ack="-", recovery="missing => default", dup="-", fresh="-",
    authority="AUTHORITATIVE: whether REAL_MANAGEMENT is active (an authority switch, not just config)",
    target="management_activation row (versioned, audited) - changes are events", strategy="DIRECT_CUTOVER",
    risk="HIGH", rollback="ONE_WAY_WITH_MIGRATION", debt="authority switch stored as an ad-hoc file", phase="P4")
add(id="TMG-06", path="trade_manager/observations/{observations,events,decisions,experiment_results}.jsonl",
    cat="RESEARCH_ARTIFACT", writers="ObservationStore", readers="research/analysis", update="append-only",
    durability="flush only", ordering="file order", ack="-", recovery="-", dup="-", fresh="-", authority="none",
    target="stays research output (or evidence store); never on operational subjects", strategy="KEEP",
    risk="LOW", rollback="NOT_APPLICABLE", phase="-")
add(id="TMG-07", path="trade_manager/prospective dashboard json (path supplied by caller)", cat="RESEARCH_ARTIFACT",
    writers="trade_manager/prospective.py", readers="operators", update="replace", durability="-", ordering="-",
    ack="-", recovery="-", dup="-", fresh="-", authority="none", target="stays", strategy="KEEP", risk="LOW",
    rollback="NOT_APPLICABLE", phase="-")

# ---------------------------------------------------------------- strategy runners (hash-frozen legacy)
add(id="CTX-01", path="context_structure_retrace_forward_state_compact.json (repo root)", cat="AUTHORITATIVE_STATE",
    writers="context runner save_state (atomic_json: tmp + replace, no fsync)",
    readers="orchestration/adapters/context_structure_retrace.discover_new_signals; runner; control_api",
    update="whole-file replace (compact projection of runner state)", durability="atomic rename, no fsync",
    ordering="none", ack="none - adapter dedupes by stable signal_id",
    recovery="runner-owned; adapter re-reads", dup="identity dedupe cache in state",
    fresh="signal created = orchestrator time at discovery, not detection time",
    authority="AUTHORITATIVE for the strategy's signals AND the signal transport to the orchestrator",
    target="the runner stays FROZEN; a read-only tailer/adapter ingests signals into PostgreSQL (signal.entry.created)",
    strategy="TAILER_INGEST", risk="HIGH", rollback="REVERSIBLE",
    debt="MULTI-ROLE: state, projection, and transport; frozen decision code, persistence outside fingerprint", phase="P1-P2",
    notes="Decision code hash covers 5 functions only; persistence may be ported later (open decision OD-05).")
add(id="CTX-02", path="context_structure_retrace_forward.jsonl (events)", cat="AUTHORITATIVE_STATE",
    writers="context runner append_event (event written BEFORE save_state)", readers="runner; control_api",
    update="append-only", durability="flush only", ordering="file order", ack="none",
    recovery="crash between event and state => event without state", dup="identity dedupe",
    fresh="-", authority="AUTHORITATIVE strategy event history", target="tailed into strategy_event/evidence tables",
    strategy="TAILER_INGEST", risk="MEDIUM", rollback="REVERSIBLE", phase="P2")
add(id="CTX-03", path="context_structure_retrace_forward_{heartbeat,manifest}.json, .pid, summary.md, /tmp/context-structure-retrace-v1-paper.stop",
    cat="DEBUG_ARTIFACT", writers="context runner", readers="control_api; scripts; operators",
    update="replace/create", durability="no fsync", ordering="n/a", ack="n/a", recovery="PID check",
    dup="-", fresh="heartbeat", authority="none (PID = process singleton)", target="runtime_instance heartbeat rows",
    strategy="DB_ONLY", risk="LOW", rollback="REVERSIBLE", phase="P1")
add(id="CTX-04", path="context_structure_retrace_forward_state.json (legacy FULL state)", cat="REBUILDABLE_PROJECTION",
    writers="none current (untracked/retired)", readers="context_structure_retrace_phase7_observer.py:124",
    update="-", durability="-", ordering="-", ack="-", recovery="-", dup="-", fresh="stale",
    authority="none", target="observer to read the compact state or the DB", strategy="RETIRE", risk="LOW",
    rollback="REVERSIBLE", debt="reader of a retired artifact", phase="P0-verify",
    notes="Open decision OD-03: is the Phase 7 observer live?")
add(id="P7-01", path="context_structure_retrace_phase7_{state,events,heartbeat,pid,manifest}", cat="RESEARCH_ARTIFACT",
    writers="phase7 observer", readers="observer; control_api", update="mixed", durability="unknown", ordering="-",
    ack="-", recovery="-", dup="-", fresh="-", authority="none - observation only",
    target="stays research; pid/heartbeat -> runtime_instance", strategy="KEEP", risk="LOW",
    rollback="NOT_APPLICABLE", phase="-")
add(id="LIQ-01", path="liquidity_displacement_*_state.json (per instance: xau-base, xau33, btc25, usdjpy25)",
    cat="AUTHORITATIVE_STATE", writers="liquidity runner atomic_write (mkstemp + fsync + replace)",
    readers="orchestration/liquidity_instances.LiquidityInstanceAdapter; runner; control_api",
    update="whole-file replace", durability="fsync + atomic rename (the only fsync'd runtime artifact)",
    ordering="none", ack="none - adapter dedupes by signal id + startup baseline",
    recovery="runner detect() re-evaluates last ~18 anchors every poll", dup="signal id",
    fresh="-", authority="AUTHORITATIVE strategy state AND transport to the orchestrator",
    target="frozen runner unchanged; adapter ingests to PostgreSQL", strategy="TAILER_INGEST", risk="HIGH",
    rollback="REVERSIBLE", debt="MULTI-ROLE: state + transport; guard (source_hash) covers liquidity_displacement.py only", phase="P1-P2")
add(id="LIQ-02", path="liquidity_displacement_*.jsonl, *_daily.jsonl, *_summary.md, *_manifest.json", cat="RESEARCH_ARTIFACT",
    writers="liquidity runners", readers="control_api; reports", update="append/replace", durability="flush only",
    ordering="file order", ack="-", recovery="-", dup="-", fresh="-", authority="none",
    target="tailed as evidence (forward-paper record); files may remain", strategy="TAILER_INGEST", risk="LOW",
    rollback="REVERSIBLE", phase="P2")
add(id="LIQ-03", path="liquidity_displacement_*.{pid,heartbeat.json} + /tmp/<prefix>.stop", cat="DEBUG_ARTIFACT",
    writers="liquidity runners", readers="control_api; scripts", update="replace/create", durability="fsync via atomic_write",
    ordering="n/a", ack="n/a", recovery="PID check-then-write", dup="-", fresh="heartbeat",
    authority="none (PID = process singleton)", target="runtime_instance heartbeat rows", strategy="DB_ONLY", risk="LOW",
    rollback="REVERSIBLE", phase="P1")

# ---------------------------------------------------------------- configuration / API / ops
add(id="CFG-01", path="orchestration/config/* (platform, risk policy, tradeability policy) and stream/binding config",
    cat="CONFIGURATION", writers="operators via git", readers="orchestrator; consumer", update="deploy-time",
    durability="git", ordering="n/a", ack="n/a", recovery="git", dup="-", fresh="-", authority="configuration (versioned)",
    target="stays a file (or versioned policy rows for operator-changeable policy); hash recorded on each decision",
    strategy="KEEP", risk="LOW", rollback="NOT_APPLICABLE", phase="-",
    notes="Any *runtime-mutable* policy must move to a DB row with a hash on each decision.")
add(id="CFG-02", path="artifacts/MT5TradingBridge_execution_build_manifest.json + cohort json (read by control_api)",
    cat="CONFIGURATION", writers="build tooling", readers="control_api", update="build-time", durability="-",
    ordering="-", ack="-", recovery="-", dup="-", fresh="-", authority="none", target="stays", strategy="KEEP",
    risk="LOW", rollback="NOT_APPLICABLE", phase="-")
add(id="API-01", path="control_api/* reads of all runtime files above", cat="REBUILDABLE_PROJECTION",
    writers="none (read-only API)", readers="operators, console", update="-", durability="-", ordering="-", ack="-",
    recovery="-", dup="-", fresh="request time", authority="none",
    target="read models over PostgreSQL (query API), never a file reader", strategy="REBUILD", risk="MEDIUM",
    rollback="REVERSIBLE", debt="control plane depends on file layout", phase="P2-P6")
add(id="OPS-01", path="scripts/backup_runtime.sh (backs up Context runner files + docs only)", cat="CONFIGURATION",
    writers="operator", readers="-", update="-", durability="-", ordering="-", ack="-", recovery="DR GAP: no runtime authority artifacts are backed up",
    dup="-", fresh="-", authority="none", target="pg_dump / PITR + JetStream stream snapshot policy", strategy="RETIRE",
    risk="MEDIUM", rollback="NOT_APPLICABLE", debt="disaster-recovery gap", phase="P1")
add(id="OPS-02", path="scripts/watch_*.py outputs (canary/live watchers)", cat="DEBUG_ARTIFACT",
    writers="watch scripts (fsync'd writes)", readers="operators", update="replace/append", durability="fsync",
    ordering="-", ack="-", recovery="-", dup="-", fresh="-", authority="none", target="stays (debug)", strategy="KEEP",
    risk="LOW", rollback="NOT_APPLICABLE", phase="-")


# ------------------------------------------------------------------ validation and rendering
COLS = ["id", "path", "cat", "strategy", "authority", "target", "writers", "readers", "update", "durability",
        "ordering", "ack", "recovery", "dup", "fresh", "risk", "rollback", "debt", "phase", "notes"]
HEAD = {"cat": "category", "dup": "duplicate_handling", "fresh": "freshness", "risk": "migration_risk",
        "rollback": "rollback_class", "debt": "architectural_debt", "authority": "authority_status",
        "target": "target_replacement", "strategy": "migration_strategy", "phase": "phase"}


def check() -> list[str]:
    errs: list[str] = []
    ids = [a["id"] for a in A]
    if len(ids) != len(set(ids)):
        errs.append("duplicate ids")
    for a in A:
        if a["cat"] not in CATEGORIES:
            errs.append(f"{a['id']}: bad category {a['cat']}")
        if a["strategy"] not in STRATEGIES:
            errs.append(f"{a['id']}: bad strategy {a['strategy']}")
        if a["rollback"] not in ROLLBACK:
            errs.append(f"{a['id']}: bad rollback {a['rollback']}")
        for c in COLS:
            if c not in a and c not in ("debt", "notes"):
                errs.append(f"{a['id']}: missing {c}")
        if a["cat"] == "EVENT_TRANSPORT" and a["strategy"] in {"OUTBOX_PROJECTOR", "TAILER_INGEST"} and not a["debt"]:
            pass
        if "MULTI-ROLE" in a["debt"] and a["cat"] not in ("AUTHORITATIVE_STATE", "CHECKPOINT", "REBUILDABLE_PROJECTION"):
            errs.append(f"{a['id']}: multi-role flag on odd category")
        if a["cat"] in ("AUTHORITATIVE_STATE", "EVENT_TRANSPORT") and a["strategy"] in ("KEEP",) and a["id"] not in {"EXE-11"}:
            errs.append(f"{a['id']}: authoritative/transport artifact marked KEEP")
    return errs


def write_csv() -> None:
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    with (DOCS / "data" / "migration_matrix.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([HEAD.get(c, c) for c in COLS])
        for a in A:
            w.writerow([a.get(c, "") for c in COLS])


def esc(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_inventory() -> str:
    by_cat = Counter(a["cat"] for a in A)
    by_debt = [a for a in A if a["debt"]]
    out = ["# 01 - Current file-IPC inventory", "",
           "> GENERATED by `tools/render_matrix.py` from the artifact records. Edit the tool, not this file.", "",
           "Baseline: `64cb03345ff386cd560fa904deb457802a2edc85`. Paths are relative to `TRADING_PLATFORM_RUNTIME_DIR` "
           "(default `<checkout>/runtime`) unless a repository-root file is shown. "
           f"**{len(A)} artifact groups** (files or file families), each classified into exactly one primary category.", "",
           "## Category totals", "", "| Category | Groups |", "|---|--:|"]
    out += [f"| {c} | {by_cat.get(c, 0)} |" for c in CATEGORIES]
    out += ["", f"**{len(by_debt)}** groups carry an architectural-debt flag (multi-role, latent defect, or coupling). "
            "A multi-role file is classified by the role whose loss would be **most damaging**, and flagged.", ""]
    for cat in CATEGORIES:
        rows = [a for a in A if a["cat"] == cat]
        if not rows:
            continue
        out += [f"## {cat}", "",
                "| ID | Artifact | Writers | Readers | Update / durability | Authority status | Debt |", "|---|---|---|---|---|---|---|"]
        for a in rows:
            out.append(f"| {a['id']} | `{esc(a['path'])}` | {esc(a['writers'])} | {esc(a['readers'])} | "
                       f"{esc(a['update'])}; {esc(a['durability'])} | {esc(a['authority'])} | {esc(a['debt'] or '-')} |")
        out.append("")
    out += ["## Per-artifact semantics (ordering, ack, recovery, duplicates, freshness)", "",
            "| ID | Ordering | Ack | Recovery | Duplicate handling | Freshness |", "|---|---|---|---|---|---|"]
    out += [f"| {a['id']} | {esc(a['ordering'])} | {esc(a['ack'])} | {esc(a['recovery'])} | {esc(a['dup'])} | {esc(a['fresh'])} |" for a in A]
    out += ["", "## Static call-site evidence", "",
            "`data/io_call_sites.csv` (from `tools/audit_file_ipc.py inventory`) lists every read/write/replace/unlink/exists call "
            "in the production modules with module, line and function. It is the mechanical cross-check for this table: "
            "a module that appears there with IPC candidates but is absent from the matrix is a matrix defect "
            "(checklist item 13 in 18).", ""]
    return "\n".join(out)


def render_matrix() -> str:
    out = ["# 04 - Artifact-by-artifact migration matrix", "",
           "> GENERATED by `tools/render_matrix.py`. Edit the tool, not this file. Full machine-readable form: `data/migration_matrix.csv`.", "",
           "There is **no mechanical ladder**. The nine-step progression "
           "(LEGACY_ONLY → DB_SHADOW_WRITE → RECONCILED_DUAL_WRITE → DB_AUTHORITY_FILE_MIRROR → JETSTREAM_SHADOW_CONSUMER → "
           "JETSTREAM_PRIMARY → LEGACY_READ_DISABLED → LEGACY_WRITE_DISABLED → RETIRED) is a *vocabulary*; each artifact picks the "
           "strategy below that is safest for it (rationale in 03).", "", "## Strategies", "", "| Strategy | Meaning |", "|---|---|"]
    out += [f"| `{k}` | {v} |" for k, v in STRATEGIES.items()]
    out += ["", "## Matrix", "",
            "| ID | Artifact | Category | Strategy | Target replacement | Risk | Rollback | Phase |", "|---|---|---|---|---|---|---|---|"]
    out += [f"| {a['id']} | `{esc(a['path'])}` | {a['cat']} | `{a['strategy']}` | {esc(a['target'])} | {a['risk']} | {a['rollback']} | {a['phase']} |" for a in A]
    tot = Counter(a["strategy"] for a in A)
    rb = Counter(a["rollback"] for a in A)
    rk = Counter(a["risk"] for a in A)
    out += ["", "## Totals", "", "| Strategy | Groups |", "|---|--:|"] + [f"| `{k}` | {tot.get(k, 0)} |" for k in STRATEGIES]
    out += ["", "| Rollback class | Groups |", "|---|--:|"] + [f"| {k} | {rb.get(k, 0)} |" for k in ROLLBACK]
    out += ["", "| Migration risk | Groups |", "|---|--:|"] + [f"| {k} | {rk.get(k, 0)} |" for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW")]
    notes = [a for a in A if a["notes"]]
    out += ["", "## Notes and open items raised by individual artifacts", ""]
    out += [f"* **{a['id']}** - {a['notes']}" for a in notes]
    out.append("")
    return "\n".join(out)


def main() -> int:
    errs = check()
    if errs:
        print("\n".join(errs))
        return 1
    if "--check" in sys.argv:
        print(f"OK: {len(A)} artifact groups")
        return 0
    write_csv()
    (DOCS / "01_FILE_IPC_INVENTORY.md").write_text(render_inventory())
    (DOCS / "04_ARTIFACT_MIGRATION_MATRIX.md").write_text(render_matrix())
    print(f"wrote {len(A)} artifact groups; categories:", dict(Counter(a['cat'] for a in A)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
