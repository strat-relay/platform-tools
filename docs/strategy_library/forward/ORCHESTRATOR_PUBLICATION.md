# Library canary canonical publication

Scope: Donchian Breakout V1, EMA Pullback V1, FVG Retracement V1, on XAUUSD,
BTCUSD and USDJPY. Run `library-canary-20261016-v1`, original activation
2026-10-16T18:15:00Z. Other library strategies remain outside publication scope.

The observer image, immutable manifest, binding, frozen evaluators and parameters
remain unchanged. The observer retains its research-table-only database role.
It never receives canonical publication or broker privileges.

The orchestrator reads accepted `research_entry` evaluations from the durable
research ledger. Only ONLINE managed instances under enabled canonical parents
load this adapter. Migration 064 creates the canonical parent rows, enables the
three existing managed instances for publication, and seeds their instrument
membership. UUIDs, version rows, frozen parameter sets and legacy `LIBRARY:`
catalog keys are preserved. API aliases prevent duplicate cards.

Publication is a completed-candle reconstruction, permanently classified
`RETROSPECTIVE_RESEARCH_EVIDENCE`, even if discovered immediately. The economic
entry timestamp identifies the candle open; information cutoff identifies the
later completed candle. Observation timestamp remains the actual processing time.
These are never timely next-open execution signals. Proposals and cancellations
stay in the research ledger. Parameter reference, fingerprint, primitive,
dependency, artifact, run and source-event provenance flow into canonical signals.

Deterministic signal identity uses instance, run and episode. The existing
orchestrator canonical publisher and its durable recovery/deduplication path own
publication. Restart discovery re-reads the source ledger; no observer checkpoint
is advanced by the adapter and no ephemeral acknowledgement is required.

The unified outcome resolver owns canonical theoretical outcomes, dispatching
`library-research-outcome.v1` to the same pure shared research policy that the
observer uses. Local observer outcomes remain a separate research projection.
No maximum hold is invented. Candle opens are retained from Redis/archive for
open-price precedence. Missing opens, missing entry candles and unobserved gaps
remain unresolved; uncertain reopening valuation and ambiguous intrabar results
retain their evidence. Broker-realized results remain separate.

The three strategy IDs are denied at execution-intent creation independently of
account allowlists. Library research signals cannot enter REAL output or shadow
execution routes. Audit and internal distribution remain enabled. Execution
eligibility and execution mode remain false/OFF in the managed instances.

The portal reports observer RUNNING/DEGRADED/STALE/NOT_OBSERVED from durable
per-stream health, with a 330-second freshness bound. This is independent of
publication enablement. Research evaluations/entries have separate counters;
canonical signals and outcomes retain the standard instance-scoped counters.
The legacy switch is hidden on research observer cards because it does not stop
the independent evaluator workload. Parent suspension pauses publication, not
observation. The strategy list refreshes every minute.

## Rollout and verification

1. Merge the platform and console PRs after CI, including PostgreSQL integration
   tests. Migration 064 must run through the existing migration workflow. Do not
   modify or reapply migration 063. Confirm slot 064 has not been allocated by a
   concurrent release before merge.
2. Deploy the new orchestrator, unified resolver and API release through GitOps,
   followed by the console release. Keep the observer pinned to its original
   image digest; do not repin or restart the experiment.
3. Confirm the orchestrator logs successful adapter initialization for three
   enabled parents/instances. If the deployment started before migration 064,
   verify configuration includes these parents before claiming activation.
4. Verify nine healthy observer streams and fresh finalized closes. A setup need
   not occur on every candle. Accepted post-T0 entries already in this run may be
   published during catch-up, always as retrospective evidence.
5. Reconcile unique accepted episodes against canonical signals by managed UUID,
   run and episode. Repeated discovery must produce no additional signals/outbox
   rows. Verify explicit parameter references and original observation timestamps.
6. Verify canonical outcome policy version, gap/ambiguity evidence and parity with
   research outcomes for identical candle coverage. Archive availability may cause
   an unresolved canonical result even when the observer retained more history.
7. Confirm no execution intents/orders for the three IDs. The page must distinguish
   actual observer health from publication and execution state.

Rollback pauses the three canonical parent rows via existing lifecycle controls
or reverts publication enablement through an audited change. Preserve ledger,
signals, outcomes, UUIDs, parameters and original T0. Already-published outcomes
continue resolving while publication is paused.

Deployment is not proven by PR merge. Kubernetes/DB verification is required.
