# Weekend observation display

The library observer's frozen runtime deliberately rejects stale completed-candle
watermarks. On 2026-10-16, BTC continued processing while XAUUSD and USDJPY became
blocked for stale completed candles after the Friday close. The status display
must distinguish expected market inactivity from an unavailable observer.

This change is a read-only API/UI projection. It does not modify the observer
file, its artifact binding, image, state, evaluator, entry rules, freshness checks,
signals or outcomes. Kubernetes readiness still follows the original frozen
observer health contract; this change does not make a NotReady pod Ready.

`library-exness-weekend-display.v1` covers only XAUUSD and USDJPY in the common
weekend window Friday 17:00 through Sunday 17:00 America/New_York. This is a
conservative expected-closure policy, not live broker-session confirmation.
Broker session metadata is unavailable. Daily rollover, holidays, unknown symbols
and later symbol-specific Sunday reopening are not inferred. BTC never inherits
this policy. The policy accounts for US DST using the packaged timezone database.

References reviewed 2026-10-16:
- https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours
- https://www.exness.com/forex/

Reclassification requires a fresh durable cycle, a ValueError-blocked stream,
successful observation within 330 seconds before the closing boundary or later,
and retained H1/M15 history reaching the last complete pre-close candle. Data
after the closing boundary invalidates the calendar assumption. Older missing
coverage, other exception types, a stale observer heartbeat, and open-session
staleness retain their degraded/stale classifications.

The API preserves raw runtime status and error type alongside the display policy
and closure boundaries. Closed instruments display MARKET_CLOSED. A mixed run
with healthy BTC and only scheduled closed instruments displays RUNNING, with
the closed instruments listed separately. Its last finalized candle comes from
active streams. If every stream is closed, the aggregate is MARKET_CLOSED.

Deploy API and console changes through their normal GitOps releases, leaving the
observer digest pinned. Confirm the actual retained closing candles satisfy the
policy before claiming live recovery. Where coverage is insufficient, the page
will continue showing DEGRADED rather than hiding a pre-existing outage.
