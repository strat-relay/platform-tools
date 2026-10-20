# Pending outcome evidence audit

Run `scripts/audit_pending_outcome_evidence.py` inside the existing resolver
container with its existing environment. It requires no new image or deployment.
The database connection enforces a repeatable-read, read-only transaction and a
20-second statement timeout. Redis operations are GET only. There are no bridge
calls, resolver leases, outcome mutations, migrations or execution changes.

The audit inventories up to 10,000 pending canonical signals, including the
signals excluded from the earlier oldest-30 diagnostic report. For each
symbol/timeframe it reads up to 20,000 archive rows and reports truncation and
source failures explicitly. Per-signal evidence and exact missing candle slots
are retained, including missing entry and trailing slots. Different archive
providers are never stitched together. Database and Redis snapshots are not
atomic with each other; the read-only database snapshot is repeatable, while
Redis is read once per stream.

`OPEN_THROUGH_LAST_EVALUATED_CANDLE` reflects stored outcome evidence; it does
not independently re-evaluate prices or guarantee current coverage. Coverage
alone does not validate OHLC revisions or executable BID/ASK evidence. Gaps are
never excused by the collector calendar: it currently ignores symbol identity
and marks weekends and three maintenance hours closed even for crypto. Broker
session evidence is required before changing outcome gap handling.

Static review also found that the runtime only fetches archive history before
the oldest Redis bar. It does not fetch archive bars inside the Redis window.
`archive_only_candles_inside_cache_window` identifies candidate missing cache
slots that could be recovered from an explicitly selected archive provider.
It does not authorize filling or rewriting any data or resolving an outcome.

After collecting the report, inspect all gap cases for archive coverage and
provider identity, inspect the remaining unspecified classifications, and
retain ambiguity unless finer causal evidence establishes price order.
