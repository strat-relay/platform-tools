# Continuous completed-candle archive

The market-data collector can opt into `MARKET_DATA_ARCHIVE_ENABLED=true`.
The default remains false. Enabling it requires an explicit primary PostgreSQL
target (`trading_platform`) and existing SELECT/INSERT rights on
`market_data.candle_archive`. No migration, grant or administrator fallback is
performed by this feature.

## Publication ordering

For each successfully validated symbol snapshot, archive all available closed
M5/M15/H1/H4 candles before advancing any Redis bar window or snapshot:

- Previously cached rows, including rows about to be evicted.
- Completed incoming snapshot rows, excluding the forming row.
- All successful timestamp-range recovery observations, including observations
  outside the final 400-bar Redis window.

Historical bootstrap uses the existing cached windows; this feature cannot
recover history already lost before activation. Existing collector bridge
requests and symbol/timeframe membership are reused. No new bridge reader,
polling workload or broker request type is introduced. Broker access stays
read-only. Strategy evaluators, execution and canonical outcome writers are
unchanged.

## Integrity and failure behavior

Validate timestamps, candle completion, OHLC geometry, finite positive prices
and lossless numeric(20,8) representation. De-duplicate identical observations;
reject different OHLC for the same identity. Missing slots remain absent and
no session calendar is used to fabricate/excuse candles. Preserve supplied
integer volume/spread metadata when representable; absent values remain NULL.
This is OHLC archival, not executable BID/ASK history.

One bounded transaction per symbol inserts missing rows, then checks existing
OHLC exactly before committing. No existing candle or original source_run_id
is overwritten. Concurrent inserts are checked too. Driver failures/conflicts
roll back all new rows from that symbol's transaction. Database statement and
lock timeouts are 10 and 2 seconds. Batches over 50,000 observations fail
explicitly rather than being truncated.

If persistence fails, retain the previous Redis candle windows and snapshot.
Record ARCHIVE_UNAVAILABLE/ARCHIVE_CONFLICT, mark data health degraded, and retry
with exponential backoff capped at 60 seconds. This adds a deliberate database
availability dependency to candle publication. Existing independent quote
polling continues; archive errors do not trip the bridge circuit breaker.
If Redis fails after archive commit, a restart/retry verifies the same bars
idempotently. A committed archive row may precede Redis publication, never the
opposite while this feature is enabled.

## Rollout and evidence

Build the tested runtime, then repin and enable only market-data-collector in
GitOps. Preserve frozen PA experiment images, identities, execution settings
and other workload images. The collector already has its database environment;
do not print connection strings or create a new privileged credential.

Verify per-symbol `md:state` archive_status=VERIFIED, fresh archive.checked_at,
and archive stream bounds/counts; md:health.archive_enabled=true; and
candle_archive_committed logs. Compare PostgreSQL max candle_time per provider
symbol/timeframe with completed cache bars. Healthy heartbeats alone do not
prove archival progress. Old original source_run_id values remain immutable.

After activation, rerun the read-only pending-outcome evidence audit and prepare
one bounded repair for the short archive-to-cache gaps created before this
rollout. Scheduled market closures and intrabar ambiguities need separate
verified evidence/policies; this change does not resolve them by assumption.

Rollback: revert the scoped GitOps image/enable flag. Already archived evidence
is retained. Disabling the feature resumes legacy Redis-only publication and
therefore resumes the risk of historical window eviction.
