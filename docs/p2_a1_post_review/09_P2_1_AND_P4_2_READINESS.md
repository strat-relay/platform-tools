# 09 - P2.1 and P4.2 readiness, resolved

## 1. P2.1: software prerequisites satisfied; deployment prerequisites still open (and were actually attempted)

**Software**: `SIGNAL_DB_PRIMARY_ENABLED`/`SIGNAL_JETSTREAM_PRIMARY_ENABLED` exist, default false, fail closed (`06`); a durable, physical-offset, restart-safe tailer with quarantine exists for JSON-parse failures (`05`, with the one content-level gap noted); a stable evaluation identity exists (`02`); a real reconciler exists (`05`); a relay with `Nats-Msg-Id` exists (`06`). **`P2_1_SOFTWARE_PREREQUISITES_SATISFIED = true`**, with the content-level malformed-input hardening (`05` section 3) recommended before or during the live window, not as a hard blocker to starting it.

**Deployment**: the repository also contains `docs/migration/p2_1_live_shadow_evidence/{README.md, preflight.json, gates.json, legacy_signal_snapshot.json}`, dated `2026-09-21T13:01:44Z`, recording that Codex actually attempted a read-only preflight against the live K8s cluster (`kubeconfig` context `local`, namespace `trading`) and **correctly stopped before attaching** `LegacySignalTailer` anywhere, because:

* the `trading` namespace has only the existing `context-paper` and `orchestrator-shadow` containers (both confirmed running, legacy signal authority active, `orchestrator_mode=SHADOW`, `live_execution_enabled=false`);
* **no dedicated PostgreSQL deployment, no dedicated NATS/JetStream deployment, and no P2 shadow worker deployment exist in that namespace**;
* the only available database/NATS instances are prior isolated local test containers, explicitly **not** treated as a live K8s shadow target (`"usable_for_live_k8s_shadow": false`) to avoid mixing environments;
* all seven A4 gates are recorded `NOT_EVALUATED` with the explicit reason "No safe live P2 shadow target was deployed; synthetic/offline evidence is not substituted for live evidence" (`gates.json`);
* zero writes, zero restarts, zero configuration changes, zero authority changes, zero broker writes (`preflight.json` `safety` block, and independently confirmed here: nothing about the live cluster is touched by this review either).

`P2_1_DEPLOYMENT_PREREQUISITES`:

1. a dedicated P2-shadow PostgreSQL role/database with migrations through `011` applied;
2. a dedicated NATS 2.10 JetStream account/stream `TRADING_CORE`, isolated from shared infrastructure;
3. a disabled-by-default P2 shadow worker, mounting the authoritative signal output **read-only**, using `LegacySignalTailer` + `ingest_signal` + the shadow consumer, exposing no execution subjects and never calling the execution consumer;
4. explicit removal/rollback manifests and credentials via Kubernetes secrets (never committed to the repository).

These are **deployment concerns**, correctly distinguished from the software contract: the software can support them (the `SignalAuthorityFlags`, tailer, quarantine, relay and reconciler are all environment-agnostic and take no K8s-specific dependency), but nothing was, or should have been, deployed by this review or by the preflight task. **`READY_FOR_P2_1_LIVE_SHADOW`** (attachment) remains **false** until deployment prerequisite 1-4 exist; the **software** that would run once attached is ready, modulo the `05` hardening recommendation.

## 2. P4.2: the stale status is resolved - **`READY_FOR_P4_2_IMPLEMENTATION = true`**

A7's prior gate was explicit: P4.2 needs at least A1-1, A1-3, A1-4, A1-5. Rechecked individually against the actual P2-A1 code (not the self-report):

| Required item | Status (this review) | P4.2 impact |
|---|---|---|
| A1-1 (stable evaluation identity) | PASS (`02`) | P4.2's `entry_signal_hash`-conflict handling (A7 `03` section 6, `10`) can now rely on a hash that means the same thing across restarts/environments |
| A1-3 (persisted EntrySignal record) | PASS (`02`) | P4.2's creation transaction can read `strategy.entry_signals` directly, as the contract in `docs/p2_p4_reconciliation/10` specifies |
| A1-4 (unambiguous strategy identity) | PASS (`03`) | P4.2's `managed_trade.strategy_ref` binding key is exactly what P2-A1 now produces |
| A1-5 (ParameterSet preserved/explicit) | PASS (`03`) | P4.2's `parameter_set_ref`/`parameter_set_status` columns map directly onto P2-A1's fields, unchanged in shape from A7's original design |

All four required items are satisfied without qualification. The two disclosed gaps elsewhere (A1-6's quarantine routing, the reconciler's lag taxonomy) are **not** among A4.2's four prerequisites and do not touch ManagedTrade creation, which reads already-persisted `entry_signals` rows and does not itself run the tailer or the reconciler.

**`P4_2_REQUIRES_P2_1_COMPLETION = false`.** P4.2 (ManagedTrade shadow) is a **separate consumer** of `signal.entry.created.v1` / `strategy.entry_signals`; A7's own dependency graph (`docs/p2_p4_reconciliation/09` section 4) places P4.2 after "P2-A1 amendment", not after the 10-day P2.1 evidence window - P2.1 and P4.2 are parallel branches from the same P2-A1 base, not sequential. Nothing in the actual P2-A1 code changes that: P4.2's creation transaction depends on the **schema and identity contract** P2-A1 provides, not on any live evidence P2.1 would collect. Requiring 10 days of live evidence before writing P4.2 (which itself runs in shadow, against no broker, producing no execution effect) would conflate a deployment-evidence gate with an implementation-readiness gate, which the task explicitly warned against ("Do not invent a new blocker merely because P2.1 evidence is not yet complete").

`P4_2_IMPLEMENTATION_CONTRACT_READY = true` (unchanged from A7 - the contract in `docs/p2_p4_reconciliation/10` was already complete and is now backed by a real schema).

**`P4_2_REMAINING_BLOCKERS`**: none from P2-A1's side. The only remaining item is the schema-completeness note from `02` (`reference_entry_semantics` not materialised in `entry_signals`) - **not a blocker**, since P4.2's own binding resolver can derive it from `entry_type` + `strategy_id` at creation time using the static mapping A7 already specified; P4.2 may optionally also request that P2-A1 add the column later as a convenience, but does not need to wait for it.
