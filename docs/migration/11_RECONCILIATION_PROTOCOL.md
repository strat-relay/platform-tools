# 11 - Reconciliation protocol

Reconciliation proves that two representations of the same fact are the same fact. Counts are a smoke signal, **never evidence**.

## 1. Identity and canonical hash

For every artifact group in `04` the reconciler needs (a) a **stable identity function** and (b) a **canonical content hash**:

| Artifact | Identity | Hash covers | Excluded (volatile) |
|---|---|---|---|
| signal | `signal_id` | strategy, instrument, direction, entry/stop/target, provenance fingerprints, evaluation/trace hash | `discovered_at`, file offset |
| route/sizing decision | `(signal_id, stream_id[, account_context_id])` | decision fields, policy hash, account snapshot ref | write time |
| execution intent | `execution_intent_id` (+ `idempotency_key`) | all fields incl. guard outcome, generation | write time |
| execution decision / attempt | `stable_id("DEC",{intent})` / `(intent, attempt_no)` | terminal state, broker ticket, error class | latency |
| ownership | `(position_id, source)` | signal/manager provenance | append time |
| broker state | `(account_context_id, snapshot_version)` | canonical positions/orders/account | `as_of` jitter policy documented |
| management proposal/intent/decision | `action_key` / ids | fields + snapshot version | write time |
| authority / generation | `(resource, generation)` | cutoff, excluded ids, armed | - |

Canonicalisation reuses the platform's existing canonical JSON (`canonical_bytes` / `canonical_hash`, V1.1) so hashes are identical across languages and runs; the checklist requires an **Evaluation hash round-trip** test.

## 2. Comparisons (all keyed, both directions)

1. **Set difference by identity**: `only_in_file`, `only_in_db`, `in_both`.
2. **Hash comparison** for `in_both`: `MATCH` / `CONTENT_MISMATCH` (shows field-level diff, never auto-repaired).
3. **Aggregate version continuity**: per aggregate, versions are contiguous; per broker account, `snapshot_version` is contiguous with no regression.
4. **Ownership generation**: file generation history ⊂ DB generation history; DB generation strictly greater than any file generation after cutover.
5. **Terminal-state comparison**: every intent/management intent is terminal in both, with the same terminal state (or an explained, allow-listed divergence such as `EXPIRED` imported).
6. **Event coverage**: every outbox row is `published_at` set; every published `event_id` appears in each subscribing consumer's inbox with an `outcome`; **state without event** and **event without state** are both findings.
7. **Projection fidelity**: projector output (file) equals the canonical serialisation of the DB rows (byte-canonical), proving the compatibility file is complete.

## 3. Windows and classifications

To avoid false positives from in-flight writes, records newer than `now - Δ` (Δ per artifact, default 2× p99 pipeline latency, min 30 s) are `PENDING_WINDOW` and re-examined next run. Every difference gets exactly one class:

| Class | Meaning | Gate effect |
|---|---|---|
| `EXPECTED_LAG` | within Δ, resolves on the next run | none if resolved |
| `MISSING_IN_DB` / `MISSING_IN_FILE` | absent on one side beyond Δ | **blocks** for authority classes; allow-list for placeholders |
| `CONTENT_MISMATCH` | same identity, different hash | **blocks** |
| `STATE_DIVERGENCE` | different terminal/aggregate state | **blocks** |
| `KNOWN_LEGACY_DEFECT` | pre-existing, documented (e.g. at-most-once observation loss, `tradeability` KeyError) | recorded; count must not grow |
| `NEW_UNCLASSIFIED` | anything else | **blocks** until classified |

No automatic repair in authority classes; repairs are explicit, audited migrations.

## 4. Outputs

`reconciliation_run(run_id, scope, started_at, source_hashes, db_hashes, verdict)` and `reconciliation_finding(run_id, class, identity, detail)`. A run is reproducible: it records the file content hash and DB high-water marks it compared. Gates (`12`) read these runs, not ad-hoc scripts. Human report = counts by class **plus** the first N findings with field diffs.

## 5. Broker reconciliation (execution)

Three-way, for `SENDING`/`UNCERTAIN`/recent `ACKED` attempts and all open positions:

```
DB attempts  ⟷  bridge lifecycle/result (via the bridge client)  ⟷  broker state (orders/positions)
```

Match by idempotency key/comment and ticket. Outcomes: found ⇒ `ACKED`/`FILLED`; provably absent (after the bridge's confirmed horizon) ⇒ `NOT_SENT`; ambiguous ⇒ remains `UNCERTAIN` and **blocks** further action on that account (fail closed) until an operator decision. The frozen order-send timeout anomaly lands here as `UNCERTAIN(BRIDGE_TIMEOUT)`.

## 6. Convergence test (checkpoint independence)

Given identical inputs, replaying (a) the file set from scratch and (b) the event stream from the beginning into an empty schema must produce the **same reconciliation hashes** as the running database. This is the executable definition of "checkpoints are not required for correctness" (`09`).

## 7. Cadence

Continuous (every minute) in shadow; before each gate a *quiesced* run (no writers) must be clean; before `T_cut` a final quiesced run with **zero** blocking findings.

Tooling to be built by the implementation stage: `reconcile` (read-only on files and DB) with the identity/hash table above as its spec. `docs/migration/tools/render_matrix.py` is the machine-readable list of artifacts it must cover; a test asserts every matrix row with authority status has a reconciler.
