# 05 - Malformed input and reconciliation, verified (with two disclosed gaps)

## 1. Malformed input: durable, observable, restart-safe - for JSON-parse failures; NOT for content-level rejections

### 1.1 What works

A line that fails to **parse** as JSON (or decode as UTF-8) is: quarantined via `platform.signal_ingest_quarantine` (`UNIQUE (source_id, source_offset, raw_sha256)`, so redelivery/replay is a no-op), reported with its physical `source_offset`, `raw_sha256` and `error`, and never converted into a `strategy.entry_signals`/`strategy.signals` row (`V10`, `V11`, `V12`). The offset advances past it (the checkpoint is written after the whole chunk, including malformed lines), so it is not re-read on a clean restart.

### 1.2 What does not work: content-level rejection crashes instead of quarantining

**Reproduced directly** (`V13`, `V14`): a line that **is** valid JSON but whose *content* `canonical_signal()`/`ingest_signal()` rejects - the concrete case tested is an epoch-only `decision_time`, which A1-6 explicitly required to be "reject[ed] ... with a quarantined finding" - instead raises `ValueError` **uncaught** out of `AppendOnlyTailer.run_once()`. Consequence:

1. the quarantine callback is never invoked (no durable record of the rejection);
2. the checkpoint is never written (it is written only after the loop completes);
3. the calling process crashes;
4. **on restart, the tailer re-reads the same poison record from the same offset and crashes again** - an infinite crash loop, not a quarantine.

This is a genuine architectural gap, not a hypothetical: it was reproduced with `AppendOnlyTailer(source, checkpoint, ingest=..., quarantine=...)` on a temporary file, no database involved. It affects the malformed-input contract's "restart-safe" and "observable" properties for this class of input, and is a literal deviation from A1-6's stated requirement.

**Practical exposure today**: the current legacy adapters always emit `decision_time`/`signal_timestamp`/`created_at` as `datetime.now(timezone.utc).isoformat()`-style strings (never epoch integers), so this path is not expected to trigger under normal live conditions. It is nonetheless a real robustness gap that should be closed - by routing any exception raised inside the `ingest` callback (not only JSON/Unicode errors) through the same `quarantine` mechanism - before or during P2.1, since a single malformed record of this class would otherwise halt the shadow ingester indefinitely rather than skip one line.

## 2. Reconciler: real and semantic, with one disclosed operational gap

### 2.1 What works

`migration/signal_reconcile.py::reconcile_legacy_signals()` reads the **actual legacy JSONL file** (not a synthetic fixture - the pre-A1 gap, A7 finding `R20`), re-derives `canonical_signal()` for every line, and compares against `strategy.entry_signals` **by `signal_id`**, on an extended semantic field set (`strategy_ref, parameter_set_ref, strategy_instance_id, instrument, direction, decision_time, entry_type, entry geometry, legacy refs, terminal_state` - `V16`), plus the `entry_signal_hash`. Findings and run summaries are persisted to `platform.reconciliation_runs`/`platform.reconciliation_findings` (`V20`), not just returned to the caller. Malformed legacy lines encountered during the comparison are recorded as `MALFORMED_LEGACY_LINE` and force `result["clean"] = False` (`V19`). This is a genuinely different, better artefact than the pre-A1 generic comparator.

### 2.2 Codex's reported taxonomy vs. the actual comparator: naming does not equal semantics

Codex's set of ten status labels (`MISSING_LEGACY, MISSING_DATABASE, HASH_MISMATCH, VERSION_MISMATCH, TERMINAL_STATE_MISMATCH, GENERATION_MISMATCH, EXPECTED_LAG, KNOWN_LEGACY_ANOMALY, MALFORMED_LEGACY_LINE, UNRESOLVED`) **is present as an enum**, matching A7's requested vocabulary literally. But **two of the ten are dead code**: `EXPECTED_LAG` and `KNOWN_LEGACY_ANOMALY` are declared in `ReconciliationStatus` and never assigned by `reconcile()`'s comparison logic (`V17`), and no code anywhere computes a time window, grace period, or tolerance (`V18` - a grep for `age|delta|grace|window|tolerance` across the reconciliation modules finds nothing).

**Concrete consequence**: `reconcile()` classifies "in legacy file, not yet in database" unconditionally as `MISSING_DATABASE` - one of A7's stated **zero-tolerance** categories (`docs/p2_p4_reconciliation/11` section 2) - with no allowance for a record that was written to the legacy file microseconds before the comparison ran and simply has not been ingested yet. A7's contract explicitly distinguishes `EXPECTED_LAG` ("legacy row newer than the lag window `Delta`... none if resolved on the next run") from the zero-tolerance `MISSING_DATABASE`; the naming exists, the distinguishing logic does not.

**Does this weaken the zero-tolerance gate itself? No, but it changes what "continuous" monitoring means.** A7's `11` requires reconciliation to run **both** continuously (every 5 minutes) **and** as **daily quiesced runs** (no writers active), with the gate (`DUAL_WRITE_RECONCILED`) keyed to **5 consecutive clean quiesced runs**. A quiesced run has no concurrent writer, so there is no in-flight record to misclassify - the missing lag-tolerance does not produce false positives there, and the gate criterion is unaffected. It **does** mean that continuous (non-quiesced) reconciliation, as literally specified, will report spurious `MISSING_DATABASE` findings for any record ingested within the polling interval, which is operationally noisy (a false zero-tolerance alarm on every run) and must be fixed - either by adding the lag classification to `reconcile()`, or by only ever running continuous checks in a mode that excludes records younger than `Delta` before calling `reconcile()` - before continuous monitoring is trustworthy. It is not fixed today.

### 2.3 Verdict

`RECONCILER_CONTRACT_PASS = true` (a real, semantic, file-vs-database reconciler now exists and is durable). `ZERO_TOLERANCE_TAXONOMY_PASS = false` (the vocabulary matches A7's ten categories exactly by name, but two of them - `EXPECTED_LAG` and `KNOWN_LEGACY_ANOMALY` - are unimplemented, so the taxonomy is not an exact operational match; the gap does not weaken the quiesced-run gate that actually decides `DUAL_WRITE_RECONCILED`, but it does mean continuous monitoring will misreport until it is closed).

## 3. What must be fixed, and what does not have to be, before P2.1

| Gap | Blocks P2.1 start? | Recommendation |
|---|---|---|
| content-level rejection not quarantined (crash loop) | **Should be fixed first** - a single malformed record of this class halts the shadow ingester indefinitely | route all exceptions from the `ingest` callback (not only `UnicodeDecodeError`/`json.JSONDecodeError`) through `quarantine`, and continue the chunk |
| `EXPECTED_LAG`/`KNOWN_LEGACY_ANOMALY` unimplemented | does not block the gate (quiesced runs are unaffected) | fix before relying on the **continuous** 5-minute reconciliation channel; not required to start the 10-day window, whose gate is quiesced-run based |
