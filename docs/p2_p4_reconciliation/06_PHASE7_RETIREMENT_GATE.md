# 06 - Phase 7 observer: role and retirement gate (resolves OD-A6-5)

## 1. Decision

The Phase 7 observer (`context_structure_retrace_phase7_observer.py`) is **not canonical** and is **neither repaired nor migrated** into the observation architecture. It is a **legacy observation producer** and a **research observer** that may stay temporarily for legacy/research compatibility until the canonical path is proven. **OD-A6-5 is resolved:** retire; do not repair; a research *consumer* of canonical observations may replace its measurement role later (not part of P4).

Evidence for not repairing (A6): it emits `bid=None, ask=None` (`M1`), is Context-only, reads the legacy full-state file the runner declares immutable (A5 `P19`), carries its own whole-file source hash (editing it changes its frozen identity), is not in the stack registry (`M23`), and its identity/lifecycle model cannot express ManagedTrade, versions, sequences or feeds.

## 2. What "temporarily remains" means

* It is **not edited, not started, not stopped, and not depended on** by any new component. P4 code never imports it and never reads its files.
* Its research artefacts (`context_structure_retrace_phase7_*`) stay classified `RESEARCH_ARTIFACT`; they may be *copies used as golden-corpus input* (A6 `19` E) but never as canonical evidence.
* Whether it is running today is a runtime fact (A6 `19`); the retirement gate does not depend on the answer.

## 3. Retirement gate (all must hold, each with evidence)

| # | Condition | Evidence |
|---|---|---|
| G1 | the Trade Observation Service is `DB_PRIMARY`/`JETSTREAM_PRIMARY` for the observation domain (A6 P4.5) with gates `DB_AUTHORITY_READY` and `JETSTREAM_PRIMARY_READY` passed | gate records |
| G2 | a **soak window** of live FORWARD observation for every open ManagedTrade with zero unexplained `OBSERVATION_GAP`, zero `LATE`/`FEED_MISMATCH` findings, and canonical decisions reconciled to the golden replay | reconciliation runs |
| G3 | the golden corpus (including the persisted Context XAUUSD replay case) reproduces byte-identical decisions under the canonical evaluator | test evidence |
| G4 | **no consumer** of the legacy observation stream remains: an access audit over `runtime/trade_manager/observation_stream/*` shows no reader (the legacy `trade_manager` stream consumer is stopped/removed from the stack registry - a change in the **bridge** repository requiring separate approval) | runtime read/write audit |
| G5 | the owner has decided what happens to Phase 7's **research measurements** (thresholds/checkpoints/hypotheses): archived as research, or re-implemented as a canonical-observation consumer - either is acceptable; neither blocks the gate | owner note |
| G6 | `PRODUCTION_FILE_IPC` static audit no longer lists the fan-out publisher/consumer as IPC (A4 `17`) | audit report |

After G1-G6: stop the observer process (operator action), remove it from any supervisor definition, delete `observation_stream/*`, `checkpoint.*.json`, `publisher_state.json`, `collector_state.json`. The observer *source file* stays in the repository under its hash (research provenance); it is simply never run.

## 4. What retirement explicitly does not require

No edit to the observer, no migration of its state, no change to the Context runner, and no P5 work (the legacy proposals/authorize/executor chain is unrelated and remains until P5).
