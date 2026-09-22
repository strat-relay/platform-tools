# 11 - P2.1 live-shadow review contract (evidence Codex must collect before any signal authority cutover)

Scope: shadow-only observation of the **existing K8s Context strategy** `CONTEXT_STRUCTURE_RETRACE_V1` on `XAUUSDm, BTCUSDm, USDJPYm, EURUSDm`, with legacy signal authority active. Required configuration throughout, asserted in every evidence report:

```
SIGNAL_DB_PRIMARY_ENABLED=false
SIGNAL_JETSTREAM_PRIMARY_ENABLED=false
```

(These flags **do not exist yet** - `R21`; P2.1 must introduce them as explicit, fail-closed configuration with default `false`; any process that finds either `true` while the gates below are unmet refuses to start.)

**Numeric thresholds are proposals for owner sign-off (`OD-A7-2`)**; the *categories* and zero-tolerance rules are architectural.

**This contract does not recommend an authority cutover from synthetic tests.** Synthetic/injected cases are *supplementary* evidence for failure modes that natural traffic may not produce in the window; they cannot substitute for natural signals and live runtime provenance.

## 0. Preconditions (P2.1 cannot start earlier)

P2-A1 (`12`) delivered: stable evaluation identity (`R1`), EntrySignal canonical record, physical `source_offset`, durable malformed-line record, real legacy-vs-database reconciler, minimal outbox relay with `Nats-Msg-Id`, the two flags, and shadow ingest packaged to run **read-only** against the K8s runtime. Collecting live evidence before P2-A1 would only measure the known defects.

## 1. Minimum evidence window

Both must be met (whichever is later), with a written extension rule if the natural rate is low:

| Measure | Proposed minimum | Rationale |
|---|---|---|
| Continuous observation duration | **>= 10 trading days** including at least one full weekend close/open and one BTCUSDm-only weekend period | covers FX weekly cycle and the 24/7 instrument |
| Natural EntrySignals ingested | **>= 30**, from **>= 3** of the 4 symbols, none of which may come from injection | statistically meaningful diversity of identity shapes (economic ids, opportunities) |
| Natural candidates/signals per instrument | >= 1 per instrument if any signal occurred for it | catches instrument-specific canonicalisation |
| Extension | if the count is not reached in 30 trading days, stop and report the observed rate; the owner decides whether a lower count with a longer duration is acceptable - the gate is *not* auto-approved | avoids gaming the threshold |

## 2. Reconciliation (keyed by `signal_id`)

Run **continuously** (every 5 minutes) and as **daily quiesced** runs (no writers for the comparison window). Compared per signal: legacy row vs `strategy.entry_signals` + evaluation, on a **semantic content hash** of:

`signal_id` (re-derived from identity components and compared), `strategy_id`, `strategy_version`, `strategy_instance_id`, canonical `instrument`, `direction`, **normalised `decision_time`**, `entry_price`, `stop_price`, `target_price`, `risk_distance`, `entry_type`, `economic_position_id`, `entry_opportunity_id`, `setup_id`, `source_event_id`, `provenance_class`, `strategy_metadata`, `signal_emitted_at`; plus terminal state, event coverage, and evaluation/trace hash linkage.

| Class | Meaning | Gate effect |
|---|---|---|
| `MATCH` | equal | - |
| `EXPECTED_LAG` | legacy row's canonical `signal_emitted_at` is no more than the configured lag window `Delta` old (proposal: 2 x 1-second tailer poll + 30 s = 32 s) | temporarily non-blocking; must resolve on a subsequent run, and does not make a reconciliation `clean` |
| `KNOWN_LEGACY_ANOMALY` | documented legacy behaviour (e.g. signals classified `GAP_RECOVERY`/`PRE_ORCHESTRATOR_REFERENCE` that are ingested but not routed) | recorded; count must not grow unexplained |
| `MALFORMED_LEGACY_LINE` | a complete line that does not parse | **acceptable only if** persisted in the quarantine table, individually explained, and never a *signal* line the strategy intended to emit; tolerance: 0 unexplained |
| **zero tolerance** | see below | **blocks** |

### Zero-tolerance categories

1. legacy signal absent from the database beyond `Delta` (`MISSING_DATABASE`);
2. database signal with no legacy row (`MISSING_LEGACY`);
3. any semantic field mismatch, including `decision_time`, geometry, instance, legacy refs, provenance class;
4. `signal_id` not reproducible from identity components;
5. **evaluation identity instability**: the same `signal_id` producing more than one `evaluation_id` (must be 0 across restarts, rotation, replays);
6. duplicate `signal_id` with differing content; duplicate events for one subject/id (beyond transport-level redelivery absorbed by the inbox);
7. state without event, or event without state (outbox coverage);
8. any future-data/outcome field present in canonical evidence;
9. any component performing a write to a legacy runtime file, a broker call, or any effect beyond shadow tables/inbox/outbox;
10. either `*_PRIMARY_ENABLED` flag true, or any consumer other than the shadow durable attached.

`MISSING_DATABASE`, `MISSING_LEGACY`, semantic/hash mismatches, event-coverage
gaps, and unexplained malformed signal lines are blocking. `EXPECTED_LAG` is
temporarily non-blocking only while the authoritative `signal_emitted_at` is
within `Delta`; after `Delta` it is `MISSING_DATABASE`. Missing or invalid
`signal_emitted_at` never receives a grace period. Quiesced reconciliations use
the same finite Delta and are strict after it expires. `MALFORMED_LEGACY_LINE`
is contextual/informational only when durably quarantined and individually
explained; otherwise it blocks. `KNOWN_LEGACY_ANOMALY` requires a separately
documented, evidenced classifier; no generic suppression is implied by the enum.

## 3. Restart, duplicate, malformed, rotation evidence (natural where possible)

| Evidence | Requirement |
|---|---|
| **Restart** | at least **3 natural restarts** of the shadow ingest process/pod over the window (pod reschedule, deploy, node event) *and* one controlled kill mid-batch on the shadow component only; after each: no lost signal, no duplicate evaluation, offset/checkpoint consistent with the database (`source_offset` derivable from DB high-water) |
| **Duplicate** | replay of an already-ingested window (from a **copy** of the source, never the live file) produces 0 new rows/events; a duplicate JetStream delivery on the live shadow durable produces 0 duplicate effects (inbox hit) |
| **Malformed / partial line** | natural occurrences are reported; in addition a partial-line and a malformed-line case are exercised on a **copy** in the same runtime image; a torn final line is never consumed early; a complete malformed line is quarantined durably (not just in memory) |
| **Rotation** | any natural rotation/truncation is recorded (`rotated=true`, prefix-hash mismatch); if none occurs naturally, one rotation is exercised on a copy; after rotation no loss and no duplicate evaluation |
| **Outbox** | every outbox row published; `outbox_oldest_unpublished_age_seconds` p99 below the agreed bound during the window; publish attempts/failures counted; relay restart tested; **`Nats-Msg-Id` present** on every published message |
| **JetStream durable / redelivery** | live durable `p2-signal-shadow` on `TRADING_CORE`: `num_pending` returns to 0, ack floor advances, `num_redelivered` explained; a forced redelivery (nack/ack-wait expiry) on the shadow durable shows inbox dedupe with **0 duplicate effects**; dedupe window boundary noted |
| **Runtime provenance** | for the **whole window**: pod/container names, restart counts, image digest and git commit of shadow ingest, relay and the observed runner; the Context runner's decision fingerprint `70dba71d...` and `assert_frozen` state; `runner` config hash; the source file path/inode/size timeline and checkpoint prefix-hash chain; PostgreSQL schema version `010+`; config dump showing both flags false; `runtime_instance_id` of every component |
| **Read-only proof** | mount/permission evidence that the shadow reads legacy files read-only; file-access audit (A4 `17` dynamic half) showing zero writes by shadow components to the legacy runtime directory; no lock files created |
| **Resource isolation** | shadow components on separate credentials/namespace/quotas; the strategy pods' CPU/latency unaffected (baseline vs window) |

## 4. Gate evidence pack (A4 gate names)

| Gate | Status expected at the end of P2.1 | Evidence |
|---|---|---|
| `SHADOW_WRITE_READY` | PASS | window + count reached; zero unexplained malformed; ingest lag p99 bounded |
| `DUAL_WRITE_RECONCILED` | PASS | **>= 5 consecutive daily quiesced runs with zero blocking findings**, final run zero |
| `JETSTREAM_SHADOW_READY` | PASS | durable/redelivery evidence above; zero duplicate effects |
| `DB_AUTHORITY_READY` | **NOT_EVALUATED** | separate authority handoff (needs fencing/lease enforcement, rollback drill, owner approval) |
| `JETSTREAM_PRIMARY_READY` | **NOT_EVALUATED** | separate event-authority handoff |
| `LEGACY_READ_RETIRE_READY` / `LEGACY_WRITE_RETIRE_READY` | **NOT_EVALUATED** | later |

Every "PASS" must cite a **reference to a stored report** (run ids, config dumps, pod evidence), consistent with `migration.gates` requiring evidence references. A gate that cannot cite natural-traffic evidence is `NOT_EVALUATED`, not `PASS`.

## 5. Report format (weekly and final)

Window, counts (ingested, natural, per instrument), reconciliation class totals by day, zero-tolerance category counts (must be 0), restart/duplicate/malformed/rotation evidence table with run ids, outbox and JetStream metrics, provenance table, flag assertions, open findings, and an explicit statement: *"No authority cutover is recommended or implied by this report."*
