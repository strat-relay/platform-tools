# 12 - P2 gaps found and the required amendment (P2-A1)

Purpose: give Codex a bounded list so nothing about P2 has to be re-interpreted. **A7 does not modify P2.** Each item cites the evidence check (`R*`) and says what it blocks. P2-A1 is small (no new domain concepts) and can run **in parallel with P4.1**.

| # | Gap | Evidence | Required change (behaviour, not code) | Blocks |
|---|---|---|---|---|
| **A1-1** | Evaluation identity is unstable: hashed provenance contains `legacy_source_reference` (path:line), `legacy_source_hash`, `as_of` | `R1`, `R12` | move all source/ingest metadata out of the hashed `Evaluation.provenance` into a **non-hashed** `source_ref` (`{path, offset}`) stored beside the evaluation; keep only strategy-provided provenance (minus the blocklist) in the hash; `evaluation_id` must be reproducible from the legacy record alone | P4.2, P2.1 |
| **A1-2** | `source_line` is chunk-relative | `R2` | derive a **physical position** (`source_offset` = byte offset of the line start, or absolute line number) from the checkpoint offset; never reset per run | P2.1 |
| **A1-3** | No persisted EntrySignal record; event lacks facts | `R4`, `R5`, `R25` | add **`strategy.entry_signals`** (`03` section 2: geometry, semantics, instance, legacy refs, `strategy_metadata`, times, `provenance_class`, references, `entry_signal_hash`); extend the `signal.entry.created.v1` payload with `entry_signal_hash, strategy_id, instrument, direction, decision_time` (facts + hash; record authoritative). Additive migration `011`-independent of P4.1's | **P4.2** |
| **A1-4** | Strategy version identity ambiguous (`'V1'` shared; per-signal `source_hash`) | `R7`, `R8` | stop inserting per-signal hashes into `platform.strategy_versions`; introduce `strategy_ref = "<strategy_id>@<strategy_version>"` on the EntrySignal record and register real source identity later from manifests/fingerprints (Context `70dba71d...`, Liquidity `source_sha256`); do **not** overwrite existing rows | P4.2 (binding key), P2.1 |
| **A1-5** | ParameterSet always NULL | `R9` | carry `strategy_metadata` verbatim; expose `parameter_set_ref = NULL` with status `LEGACY_IMPLICIT_IN_STRATEGY_ID` | P4.2 |
| **A1-6** | `decision_time` string not normalised | `R10` | normalise to UTC ISO (`...Z`, microseconds) before hashing/persisting; reject epoch-only values with a quarantined finding | P2.1 |
| **A1-7** | Malformed complete lines are consumed and only in memory | `R3` | persist each malformed line (source ref, error, raw bytes hash) in a durable quarantine/finding table | P2.1 |
| **A1-8** | No real legacy-vs-DB reconciliation | `R20` | a reconciler that reads the legacy signal file (read-only) and the DB, computes the semantic content hash of `11` section 2, and persists `platform.reconciliation_runs/findings` | P2.1 |
| **A1-9** | No outbox relay; no `Nats-Msg-Id` | `R17`, `R18` | minimal relay: lease, per-aggregate ordered selection, publish with `Nats-Msg-Id = event_id`, publish-ack then `mark_outbox_published`, failure accounting; `JetStreamPublisher` header support (shared with P4.1) | P2.1, P4.3 |
| **A1-10** | `SIGNAL_*_PRIMARY_ENABLED` flags absent | `R21` | explicit fail-closed configuration, default false, asserted in status output | P2.1 |
| **A1-11** | Canonical-result uniqueness missing; candidate event filed under the signal aggregate | `R22`, `R5`, `R6` | unique constraint on `(strategy_id, strategy_version, coalesce(parameter_set_ref,''), instrument, decision_time, candidate_id)` for `strategy.entry_signals`; candidate event `aggregate_type = candidate`, `aggregate_id = candidate_id` | non-blocking hardening |
| **A1-12** | Allowlist rather than blocklist for provenance | `R12` | replace the blocklist of future/outcome keys by an allowlist of strategy-provided provenance keys | non-blocking hardening |
| **A1-13** | No golden vectors for legacy `signal_id`s | checklist item 1 | golden test: legacy identity components -> expected `signal_id` for both adapters (documentation of the passthrough contract) | non-blocking |

## What is deliberately **not** in P2-A1

Any authority cutover, publication concept, ManagedTrade code, execution/bridge change, StrategyHost, or `strategy_versions` registry design (only the non-collision rule of A1-4).

## OD-01 repair (informational)

P2 repaired `tradeability_decisions` (`R16`). A7 records it as a deliberate, documented, test-covered production change (`02` item 14: FAIL against A6's literal criterion, no P5 crossing). It is **not** reverted or re-reviewed here; the behavioural question A5 raised (a policy that has never been applied in REAL now runs in the orchestrator's analytical path) is `OD-A7-5` for the owner.

## Order of work

`P2-A1` (A1-1..A1-10 required; A1-11..13 recommended) || `P4.1` -> `P4.2` (needs A1-1, A1-3..A1-5) -> `P2.1` (needs A1-1, A1-2, A1-6..A1-10).
