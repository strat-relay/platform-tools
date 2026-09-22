# P2-A1 post-implementation review (A7.1)

Agent CLAUDE-A7.1-P2-A1-POST-IMPLEMENTATION-REVIEW. Committed against A7 architecture commit `cee5f31` (`/Users/caleb/trading-platform-claude-a7`, unmodified). The actual P2-A1 implementation was inspected in a **separate detached worktree** at `3a39fd7` (`/Users/caleb/trading-platform-p2a1`, created for this review, not part of this repository's history) so that the A7 documents this review checks against stay untouched, per instruction. Provenance commit `8bd48f9` (import of the A7 reconciliation contract into the P2 lineage) and `ca1ec28` (the P2.1 preflight evidence Codex separately recorded) were read but not modified. **This is a focused verification, not a new architecture study.** No production, strategy, Trade Manager, bridge, Kubernetes, or PostgreSQL/NATS runtime state was changed by this review; `BROKER_WRITES=0`.

## Headline verdict

**Codex's self-report is substantially accurate but overstates two items.** Of the 13 requirements: **9 of the 10 required items (A1-1..A1-10) are a clean PASS**; **A1-6 is PARTIAL** (decision_time normalisation is correct; the required "quarantined finding" for a rejected epoch-only value is not implemented - it crashes the ingest process uncaught instead, reproduced directly); the 2 recommended items Codex marked implemented (A1-11, A1-12) are confirmed correct; A1-13 is confirmed not implemented, as Codex itself reported, and remains non-blocking. A related, previously-unflagged gap was found and reproduced: the reconciler's `EXPECTED_LAG`/`KNOWN_LEGACY_ANOMALY` categories exist by name but are never assigned by the comparison logic - the taxonomy matches A7's vocabulary but not its semantics.

Despite these two gaps, **ADR-0004's implementation prerequisites are met**: `CAN_MANAGED_TRADE_BE_CREATED_FROM_CANONICAL_ENTRY_SIGNAL = true`, **P4.2 is unblocked** (`READY_FOR_P4_2_IMPLEMENTATION = true`), and **P2.1's software is ready** while its **deployment is not** (confirmed by a real, read-only K8s preflight that correctly stopped before attaching anything, because no dedicated PostgreSQL/NATS/shadow-worker exists in the live namespace).

## Documents

| # | Document | Task section |
|---|---|---|
| 01 | [`01_A1_ITEM_VERIFICATION.md`](01_A1_ITEM_VERIFICATION.md) | 1 |
| 02 | [`02_EVALUATION_IDENTITY_AND_ENTRY_SIGNAL.md`](02_EVALUATION_IDENTITY_AND_ENTRY_SIGNAL.md) | 2, 3 |
| 03 | [`03_STRATEGY_VERSION_PARAMETER_PROVENANCE.md`](03_STRATEGY_VERSION_PARAMETER_PROVENANCE.md) | 4, 5 |
| 04 | [`04_FORWARD_TIMING_OD_A7_4.md`](04_FORWARD_TIMING_OD_A7_4.md) | 6 |
| 05 | [`05_MALFORMED_INPUT_AND_RECONCILIATION.md`](05_MALFORMED_INPUT_AND_RECONCILIATION.md) | 7 |
| 06 | [`06_OUTBOX_RELAY_AND_FLAGS.md`](06_OUTBOX_RELAY_AND_FLAGS.md) | 8, 9 |
| 07 | [`07_MIGRATION_011_REVIEW.md`](07_MIGRATION_011_REVIEW.md) | 10 |
| 08 | [`08_A6_CHECKLIST_RERUN.md`](08_A6_CHECKLIST_RERUN.md) | 11 |
| 09 | [`09_P2_1_AND_P4_2_READINESS.md`](09_P2_1_AND_P4_2_READINESS.md) | 12, 13 |
| 10 | [`10_CONTRACT_DRIFT_CHECK.md`](10_CONTRACT_DRIFT_CHECK.md) | 14 |
| - | `tools/verify_p2_a1_post_review.py`, `data/p2_a1_evidence.json` | 34 re-runnable checks against the P2-A1 commit |

## What changed since A7's `docs/p2_p4_reconciliation`

That directory is **not modified**. This review supersedes its P2-readiness conclusions **prospectively**, as follows:

| A7 conclusion (`8f49aef`) | This review's update (`3a39fd7`) |
|---|---|
| `READY_FOR_P2_1_LIVE_SHADOW = false` (before P2-A1) | software now ready; **deployment** still not (unchanged reason, now with live preflight evidence) |
| `READY_FOR_P4_2_IMPLEMENTATION = false` (after P2-A1 items A1-1,3,4,5) | **true** - those four items are confirmed delivered |
| A6 checklist 12 PASS / 6 FAIL / 2 N-A | 15 PASS / 2 PARTIAL / 0 FAIL / 2 N-A / 1 SUPERSEDED (`08`) |
| OD-01 repair treated as a disclosed FAIL against the literal A6 checklist | treated as **SUPERSEDED_BY_APPROVED_DECISION**, per A7's own acceptance and this review's explicit instruction not to keep re-litigating it |

## Two findings not present in Codex's report

1. **A1-6's "quarantined finding" for epoch rejection is not implemented.** `AppendOnlyTailer.run_once()` only catches `UnicodeDecodeError`/`json.JSONDecodeError` around the ingest call; any exception the `ingest` callback itself raises (including `canonical_signal`'s deliberate `ValueError` on an epoch-only `decision_time`) propagates uncaught, the checkpoint is never written, and a restart re-reads and re-crashes on the same record. Reproduced directly with no database involved (`05`). Not expected to trigger under current legacy data shapes, but a real robustness gap and a literal deviation from the stated requirement.
2. **The reconciler's `EXPECTED_LAG`/`KNOWN_LEGACY_ANOMALY` categories are dead code.** They exist in `ReconciliationStatus` but `reconcile()` never assigns them; no time-window/lag-tolerance logic exists anywhere. This does not weaken the gate that actually matters (`DUAL_WRITE_RECONCILED`, based on quiesced runs with no concurrent writer) but means the "continuous every-5-minutes" reconciliation channel A7 specified will misreport in-flight records as zero-tolerance `MISSING_DATABASE` findings until fixed.

Neither finding blocks P4.2. The first should be fixed before or during P2.1's live window; the second should be fixed before relying on continuous (non-quiesced) reconciliation, but not before starting the quiesced-run-based evidence window.

## Open items carried forward

| Item | Status |
|---|---|
| Route all `ingest` exceptions (not just decode/JSON errors) through `quarantine` | recommended fix, not yet done (P2-A2 candidate) |
| Implement `EXPECTED_LAG`/`KNOWN_LEGACY_ANOMALY` classification with a configurable `Delta` | recommended fix, not yet done (P2-A2 candidate) |
| `reference_entry_semantics` materialisation | optional; derivable by P4.2 without it |
| A1-13 golden vectors | still not implemented; still non-blocking per A7 |
| P2.1 deployment (dedicated PostgreSQL, dedicated NATS, shadow worker) | not deployed; preflight-only; separate infrastructure task |
| OD-A7-4 (FORWARD eligibility lag bounds) | still open; correctly does not block P2.1 collection or P4.2 (`04`) |

## Tooling

```sh
P2A1_ROOT=/Users/caleb/trading-platform-p2a1 PYTHONDONTWRITEBYTECODE=1 \
  python3 docs/p2_a1_post_review/tools/verify_p2_a1_post_review.py          # table; exit 1 on FAIL
... --write   # also data/p2_a1_evidence.json
```

Requires a detached worktree at `3a39fd783874303d8d526c7d6c52387c43925928` (create with `git worktree add --detach <path> 3a39fd783874303d8d526c7d6c52387c43925928` from the `trading-platform` repository) so the P2-A1 code can be imported and exercised without moving this repository off its A7 architecture commit. Pure P2-A1 functions on temporary data plus `git` reads; no database, NATS, broker, runtime file, or Kubernetes access.

## Limits

Static and behavioural review of pure functions only; no database-backed test (`ingest_signal`, `persist_evaluation`, `OutboxRelay.publish_batch`, `SignalShadowConsumer`) was executed against a real PostgreSQL/NATS instance in this review - their SQL and control flow were read and cross-checked instead. The live K8s preflight evidence (`09`) was produced by a separate, earlier task (`ca1ec28`) and is cited, not reproduced, here.
