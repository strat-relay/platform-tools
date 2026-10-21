# Library next-open quotes and publication latency

The original library canary reconstructs entries from completed candles. The
observed FVG signal `SIG_ac203f6ae8ae97bde9f2842f` has reference time 14:15 UTC,
information cutoff 14:30 UTC, observer time 14:30:22.665308 UTC, and reported
publication at 14:30:24.828992 UTC on 2026-10-21. Its 15-minute interval is a
reconstruction requirement. The additional reported intervals are 22.665308s
from information cutoff to observer time and 2.163684s from observer time to
publication. These figures alone do not allocate collector, polling, evaluation,
transaction or relay costs.

The existing GitOps canary command uses `--interval 60`. Its previous scheduler
also slept that interval after processing, adding processing duration to the
cadence. A 60-second polling interval is a confirmed configured latency source,
not proof that polling accounts for every second in this individual signal.

## Fresh-run entry contract

Use a new reviewed run identity, released artifact, future activation boundary,
and matching managed publication binding. Do not edit a frozen run manifest or
reuse its checkpoint with changed adapter/source/configuration. Existing runs
and their historical signals keep their existing evidence classification.

For the fresh run, include these manifest settings:

```json
{
  "entry_mode": "CONTEMPORANEOUS_CANDLE_OPEN",
  "entry_quote_max_age_seconds": 5
}
```

For an interim polling canary, use `--interval 1`. This is a mitigation, not the
target event-driven architecture. Cycles are sequential; processing duration
counts toward the interval and overruns do not create overlapping work. The
collector remains the sole broker reader; this observer reads Redis only.
Default reconstruction mode and the default CLI interval remain compatible
with historical configuration; merely updating an image will not switch an old
canary to the fresh entry contract.

At the setup close/next-open boundary, the shared evaluator can resolve a
pending entry using the provider's actual opening observation, without receiving
the completed entry candle. When quote capture is enabled, it also requires a
real hot-cache quote: finite positive bid/ask, ask >= bid, matching provider if
the quote declares it, quote timestamp at/after the entry boundary, receipt at/
after the provider timestamp, and no future or stale timestamps. Missing or
invalid quotes fail the stream transition atomically, allowing a valid retry
within the existing 60-second opening window. Late completed-candle fallback
remains retrospective research evidence.

The accepted research event and canonical strategy metadata freeze `entry_quote`:
bid, ask, spread, provider timestamp, cache receipt timestamp, observer timestamp,
quote age, seconds after the opening boundary, and tentative entry side/price.
LONG uses ASK; SHORT uses BID. A quote observed several seconds after the boundary
is timestamped as such, never relabelled as the exact historical opening quote.

The canonical paper `entry_price` and theoretical resolver still use the frozen
provider candle-open reference. `entry_quote.tentative_entry_price` is a distinct
contemporaneous execution reference, not a broker fill or net-cost performance
measurement. Using bid/ask as the actual theoretical entry/outcome basis would
require a separately versioned outcome/entry policy and geometry validation;
this change does not silently reinterpret old R results.

## Event-driven target

Existing JetStream contracts cover downstream canonical signals and execution.
The collector currently writes Redis without market-event subjects, and the
library observer and adapter poll Redis/PostgreSQL. A separate staged change
should introduce durable market-data notifications and research-entry
notifications: collector commits market evidence, emits versioned events, the
observer consumes those events using the same pure evaluators, commits its
checkpoint/evaluation and durable publication intent atomically, and notifies
the canonical orchestrator after commit. Acknowledge consumption after durable
effects, deduplicate by event and economic episode, and recover on restart.

Bar-close, opening-price and quote events must retain their own source,
availability and receipt timestamps. Consumer arrival order must never replace
H1/M15 causal watermark checks. An opening arriving before its setup context
must wait for that context, with original timestamps preserved. Event payloads
must refer to immutable evidence/snapshot versions; reading a later mutable
Redis value during replay cannot stand in for an earlier quote.

Keep research-entry notifications outside execution-intent subscriptions and
retain the orchestrator's research/age/eligibility gates. Polling may remain for
reconciliation/health. Merely enabling the existing dormant NATS-first signal
data-plane mode does not wire market data or research observers to NATS, and is
not part of this patch. Stream ownership, retention, ACLs, resource sizing and
GitOps ingress/egress need review before activation.

## Stage evidence

| Stage | Evidence |
| --- | --- |
| Collector request/response | snapshot `source_timing.collector_request_started_at`, `collector_response_received_at` |
| Cache publication | `collector_cache_write_started_at`; log `market_snapshot_cached` adds completion timestamp |
| Observer batch/lock | event `stage_timing.cycle_started_at`, `lock_acquired_at` |
| Snapshot read | event `snapshot_read_started_at`, `snapshot_read_completed_at` |
| Evaluation | event `evaluation_started_at`, `evaluation_completed_at` |
| Ledger write and commit | `library_observer_batch_committed` log with run/event IDs, write start/end and `transaction_committed_at` |
| Orchestrator discovery | `library_signal_discovered` log with run/event/signal IDs and read start/end |
| Canonical persistence | existing `SIGNAL_DB_PERSIST_STARTED` and `SIGNAL_INGESTED` trace events, with frozen source stage timing attached to the start trace |
| Transport publication | existing `OUTBOX_PUBLISH_ATTEMPT`, `OUTBOX_PUBLISHED`, `OUTBOX_FAILED` trace events |

Observer source stage timings flow into canonical `strategy_metadata.publication_timing`.
Changing retry/discovery/commit timings remain correlated log evidence; they are
not inserted into immutable signal content and cannot change its identity on retry.
Commit is logged only after the connection context exits successfully. Future
log aggregation must retain those correlated logs if a complete historical
breakdown is required. Missing stages remain unknown; do not assign them zero.

Cross-process wall clocks require alignment. PostgreSQL `now()` defaults are
transaction-start timestamps, not wall-clock measurements of insertion or
network publication, so an `ingested_at` preceding emission does not by itself
prove clock skew. Prefer the stage trace timestamps for latency attribution.
No source stage timings can be recovered retroactively for this original FVG
signal if historical logs did not record them.

## Rollout and verification

This source patch does not register, activate, repin, restart or deploy workloads.
Build a verified immutable image, provision the fresh run using the existing
approved registration/activation path, and update its isolated GitOps workload
with the next-open settings, quote freshness and one-second interval. Verify both
PostgreSQL and Redis ingress/egress allowances for its actual pod labels.
Preserve existing observers and checkpoints during review/cutover.

Verify that an accepted research entry appears before its entry candle closes,
that bid/ask and all three quote timestamps are present, that long/short tentative
prices use the correct side, and that all stage timings can be joined by run,
event and signal IDs. Verify restarts do not duplicate or reprice the observation.
Measure the observed distribution before setting a publication latency target;
a one-second poll does not promise one-second end-to-end publication. Execution
remains disabled/ineligible and broker writes must remain zero.
