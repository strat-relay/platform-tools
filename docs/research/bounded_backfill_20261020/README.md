# Bounded recovery for pending theoretical outcomes

Source: `pending-outcome-evidence-20261020T035537Z.json`, observed
2026-10-20T03:55:40.785867Z. `plan.json` binds the canonicalized evidence digest
and the 26 affected signal IDs. This does not change strategy identities,
rules, outcome contracts, execution authority or observer deployments.

Approved recovery scope: 6 symbol/timeframe streams, 18 deduplicated half-open
intervals, 4,979 possible candle slots, and at most 35 requests of 200 slots:

| Stream | Missing slots requested | Requests |
|---|---:|---:|
| BTCUSDm M15 | 366 | 2 |
| ETHBTCm M15 | 408 | 3 |
| USDJPYm M5 | 1,621 | 10 |
| USDJPYm M15 | 357 | 2 |
| XAUUSDm M5 | 1,740 | 10 |
| XAUUSDm M15 | 487 | 8 |

Some slots may be market closures. The tool does not excuse them, synthesize
bars or promise 4,979 returned observations. Missing observations keep each
interval PARTIAL. A COMPLETE backfill interval means every requested candle
slot was fetched, not that the strategy outcome is resolved.

## Execution

Run `scripts/bounded_outcome_backfill.py` in the existing resolver image via
Python stdin/code transport, with `PYTHONPATH=/app`. Feed `plan.json` on stdin
and require `--expected-plan-sha` matching its hash. `--apply` authorizes archive
inserts; without it, fetching and validation perform no database mutations.
The legacy backfill tool is deliberately not invoked: its dry run creates a
run record and its session heuristics ignore symbol identity.

Use the resolver's existing database environment and the collector's configured
read-only bridge URL, `http://10.10.10.100:22347/mcp`. The only RPC method is
`mt5_rates_range`, with completed bars requested. No retries, parallel bridge
calls, Redis writes, broker orders, canonical outcome writes, migrations,
Secrets or workload changes occur. Requests are sequential, spaced by at
least one second, with a 300-second total fetching budget and 30-second maximum
per-request timeout. Database identity must be `trading_platform` on a primary.

All responses must pass timestamp, timeframe alignment, ordering, duplicate,
finite-positive-price, archive precision, OHLC geometry and page-bound checks
before archive mutation starts. Inclusive upper-bound observations, if returned,
are validated and explicitly excluded from the half-open recovery interval.
The report retains normalized fetched OHLC observations and their digest.

Writes are one atomic transaction: append missing archive bars under provider
MT5 and deterministic per-interval run IDs; compare existing OHLC exactly;
abort and roll back all writes on any conflict. Existing archive prices are
never overwritten. Re-running an unchanged recovery is idempotent, although
it re-fetches the bounded pages. Empty or incomplete pages remain partial.

Required existing rights: SELECT/INSERT on `market_data.candle_archive`, and
SELECT/INSERT/UPDATE on `market_data.backfill_runs`. Preflight runs in a read-only
connection before any bridge requests. Missing privileges fail closed; the
tool never grants itself privileges or substitutes administrator credentials.

## Verification and limits

Preserve stdout JSON and stderr progress separately. After ARCHIVE_APPLIED,
rerun the pending evidence audit and inspect resolver progress. The resolver
can use newly available prefix history; it currently does not query archive
holes inside the Redis window. Session-gap and intrabar ambiguity handling
remain unchanged. Archiving cannot repair the four gold cases whose only
missing interval is an actual session closure unless verified session evidence
supports a separate policy repair. No unavailable broker cost/BID-ASK inputs
are invented. Historical bars fetched now retain their new fetched_at timestamp;
they must not be presented as evidence observed prospectively at trade entry.
