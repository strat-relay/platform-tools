# Resolver queue starvation and full-path replay correction

User-supplied read-only cluster evidence captured 2026-10-16T16:08:58Z shows
123 eligible unresolved signals: the oldest 100 have CANDLE_GAP state, while
23 later signals have no resolver state. Individual attempt counts reach 58,414.
The deployed query orders only by original decision time and limits to 100.
This establishes queue starvation in that snapshot, not the cause of the earlier
orchestrator heartbeat outage.

## Correction

Select by oldest durable attempt timestamp, using decision time for unseen rows,
with deterministic decision-time/signal-ID tie breaks. Existing transactional
state persistence advances attempt time, so blocked rows yield to waiting work.
No migration, batch-limit increase, new worker, or alternate outcome writer.

The pure candle resolver initializes entry and activation state on every call.
A durable close cursor does not capture sufficient state to resume it on a candle
suffix. Replay the available path from the immutable entry, including archived
history preceding Redis. Query archive even when Redis is empty. Preserve proven
ambiguity and unfilled-expiration classifications. Do not retain an old cursor
beyond a newly detected gap; persist only evaluated pre-gap coverage.

This deliberately trades more archive reading for correctness. Benchmark query
latency and monitor queue rotation before expanding workloads. Archive/Redis
conflict reconciliation, missing executable BID/ASK history, and market closure
policy remain separate concerns. No missing candles, fills, or outcomes are invented.

## Rollout and evidence repair

This source change does not deploy anything or mutate existing production state.
After image build/review, deploy only the resolver via GitOps and verify all 123
eligible signals receive attempts rather than repeating a fixed oldest batch.
Monitor by signal, reason, attempt timestamp, and coverage, not just status=OK.
Snapshot existing resolver evidence before authorized rollout: normal processing
can replace invalid derived cursors with newly recomputed coverage. Existing
terminal outcomes are not selected or rewritten. Missing history remains open.
Do not bulk-reset cursors or force outcomes. Recover historical data separately
only with independently verified mappings, timeframes and source provenance.
Theoretical and broker-realized result ownership stays separate.
