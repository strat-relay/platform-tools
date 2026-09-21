# 11 - P2 / P3 / P4 / P5 dependency assessment

Question from the task: does anything in OD-05 / OD-06 change A4's ordering, and can P2 proceed while OD-06 is open? Expectation to verify, not assume: *P2-P4 can proceed, P5 blocked by OD-06.*

## 1. Verdict

| Phase (A4 `docs/migration/16`) | Blocked by OD-06? | Blocked by OD-05? | Other gates found here | Verdict |
|---|---|---|---|---|
| **P2 signals** | **No** - no broker-write path exists in the signal plane | **No** - the runner-to-adapter file hop is the P2 mechanism (S0); S1 is later | reconcile the OD-01 output as-is (`SKIPPED` sizing rows); projector must be append-only and stable (section 3) | **May proceed** |
| **P3 broker state** | **No** for shadow/tailing | No | authority cannot move before P5 (writer is the execution consumer); extra reads compete for the single EA queue (section 4) | **May proceed as shadow only** |
| **P4 management / observation** | **No** for proposals, decisions, activation, observation transport | No | producer of the observation stream is the optional Phase 7 observer (OD-03); ownership authority cannot move before P5; REAL management *execution* is unavailable at the bridge (`B4`) | **May proceed in part** |
| **P5 execution** | **Yes** | Not directly (signals arrive via S0) | bridge write ledger/status/fence; platform schema deltas; retcode table verification; halt policy sign-off; management-write capability | **Blocked** |

`READY_FOR_P2_P4 = true` (with the constraints below); `READY_FOR_P5 = false`.

## 2. Why OD-06 does not touch P2-P4 (verified, not assumed)

* The orchestrator's read provider is limited to read-only tools (`orchestration/brokers/mt5_shadow.py`, `READ_ONLY_BRIDGE_TOOLS`, includes `mt5_order_check` which is a read: `01`), and `signal_orchestrator.py` carries a broker-write safety audit; `route_signal`'s `order_check` call is dead behind the OD-01 exception in any case.
* The Trade Manager never imports an execution provider ("Trade Manager never imports this function and never has an execution provider", `live_execution_consumer.py:126-131`); it only authorises.
* All broker writes are in `live_execution_consumer.py` (`process_intents`, `process_management_intents`) and the smoke commands. Nothing in P2-P4 changes those files' behaviour *unless a phase moves an execution-plane artifact* - which is exactly what section 3 shows must wait for P5.

## 3. A4's writer assumptions that do not hold, and the resulting re-ordering

Who actually writes each artifact (`P7`, `P8`, source reading):

| Artifact (A4 id) | Writer today | A4 plan | Corrected plan |
|---|---|---|---|
| `signals.jsonl` (ORC-01) | orchestrator (platform code) | P2 outbox/projector | **as planned** (P2). The file consumer keeps reading the projected file |
| `route_decisions`, `sizing_decisions` (ORC-02/03) | orchestrator | P2/P3 | as planned; `sizing_decisions` is **analytical in REAL** (`P11`), risk rating drops; still consumed by DEMO/DRY_RUN branches |
| `management_proposals`, `management_decisions` (MGT-03/05) | Trade Manager `stream_consumer`; `central.authorize_pending_proposals` (called from the orchestrator loop, `signal_orchestrator.py:590`) | P4 | as planned; `authorize()` **reads** ownership and broker state, which stay file-authoritative until P5 (rule below) |
| **`ownership_registry.jsonl` (MGT-01)** | **the execution consumer** (`record_real_execution`, `record_management_result`); TM only reads | P4 `OUTBOX_PROJECTOR` | **Shadow (tailer) in P4; authority moves with P5** in the same `T_cut` as execution. An outbox cannot be added to a writer that is legacy execution code |
| **`broker_state.json` (MGT-02)** | **the execution consumer loop** (`refresh_from_provider`) | P3 single new publisher | **Shadow/tailer in P3; authority with P5.** A separate publisher writing authority while the consumer also refreshes the file would be two writers; stopping the consumer's refresh is an execution-process change and belongs to P5 |
| `management_intents.jsonl` (MGT-04) | `central.authorize` | P5 with executor | as planned (P5) |
| observation `events.jsonl` (TMG-01) | **Phase 7 observer only** (`P27`) | P4 replace by events | P4, **conditional on OD-03 (OD-A5-13)** - the producer must be declared first |
| execution intents/decisions, `real_*`, resume, smoke | consumer | P5 | as planned |

**Rule for P4 `authorize()`:** while ownership and broker state are file-authoritative, `authorize()` reads the files; it may read the DB shadow only after that artifact's `DUAL_WRITE_RECONCILED` gate passes, and never a mix within one authorisation.

**Execution-plane authority set.** The following switch together at `T_cut(execution:real:<account>)` (A4 `08`): execution intents/decisions/attempts, `real_state`/resume generation, position ownership, broker-state authority, management-intent results. This is a *larger* set than A4's P5 list and it is why P5 is the most conservative phase.

## 4. Constraints for the phases that may proceed

**P2**

1. The projector regenerating `signals.jsonl` must be **append-only, deterministic, and preserve `created_at`, `signal_timestamp`, `provenance`** exactly: `create_intents` (REAL) filters by `created_at`/`signal_timestamp` against the resume cutoff, `excluded_signal_ids` and the consumer's *startup baseline* (`startup_signal_ids` computed from the file at consumer start, `live_execution_consumer.py:1945`). A projector that re-emits or reorders historical rows could release old signals.
2. The replay-guard watermark (`startup_epoch.json`) is a safety boundary; it is imported, not regenerated (A4 `ORC-11`).
3. OD-01: reconcile against the current behaviour; no silent fix.

**P3**

1. Shadow only: tailer of `broker_state.json` and/or an independent *read-only* publisher writing shadow rows.
2. **Read budget.** All reads go through one EA queue (`MAX_QUEUE_DEPTH` 32) served one request per poll cycle (default floor 5 s, `B9`) and writes carry a 5 s max-age. Additional reads of class `EXECUTION_CRITICAL` can push `order_check`/send past their deadline (fail-safe: they expire, but fills are lost). New readers must use a lower priority class and a measured budget (`/health` exposes latency by class).

**P4**

1. Proposals/decisions/activation may go DB-primary; **REAL management execution stays with the file consumer** and, at this baseline, cannot succeed (`B4`).
2. Observation fan-out: decide the producer first (OD-A5-13). Migrating a stream whose only publisher is an optional, stale-input observer risks migrating something that is not part of the intended system.
3. The at-most-once checkpoint behaviour (A4 `TMG-03`) is fixed by the JetStream durable consumer; it changes observable behaviour (no more silent loss) and should be recorded as such.

## 5. What P5 needs before it can start (all from this analysis)

| # | Prerequisite | Where |
|---|---|---|
| 1 | OD-06 design **approved** (`15`) | ADR |
| 2 | Bridge change implemented and tested: fence, authorization verification, write ledger, `write-status`, `write-cancel`, persisted fence, `bridge_epoch`, `/health.write_fence`; REAL reduce-only capability if management execution is in scope | `12` |
| 3 | Platform: `assert_generation`, lease liveness/renewal, attempt table + transition function, partial unique index, account halt, fence authority, `BrokerWriteGate` on the real call path (retire raw urlopen) | `05`, `07` |
| 4 | Retcode classification verified (`06` 5) | `06` |
| 5 | Owner decisions OD-A5-4 (halt policy), OD-A5-6/7 (bridge scope, break-glass), OD-A5-10 (EA read enrichment) | README |
| 6 | Shadow intent comparison window (A4 gates) | A4 `12` |
| 7 | Rollback drill with generation monotonicity | A4 `13` |

## 6. Does OD-05 change any ordering?

* **P2 is unaffected.** S0 (tailer) is the P2 mechanism.
* **S1 (StrategyHost) is a separate track**, independent of P3-P5. It must complete for a runner *before that runner's* file output can be retired (A4 P6 `LEGACY_WRITE_RETIRE_READY`). It should not run in parallel with P5 on the same instance: one authority change at a time.
* The Phase 7 observer's input file and its role as sole TM upstream (`P27`) mean OD-05 S1 for Context must be sequenced **after** OD-A5-13 is decided.
