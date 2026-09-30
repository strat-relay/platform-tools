# Broker-aware executed TP/SL

The strategy remains the owner of canonical entry, stop, target, risk, and R:R.
Those values are reference-market (Bid-side chart) values and are never rewritten
for a broker.

The bridge proves the price semantics used here:

- MT5 `CopyRates` supplies the chart/reference OHLC used by the strategy.
- A market BUY enters at Ask; a market SELL enters at Bid.
- A BUY position exits on Bid; a SELL position exits on Ask.

Consequently the submission-time translation is:

| Position | Broker TP | Broker SL |
| --- | --- | --- |
| BUY | canonical level | canonical level |
| SELL | canonical level + spread | canonical level + spread |

Final levels are rounded to the symbol tick size. Stops and freeze-level
constraints are checked against the executable side; an invalid translation
fails closed rather than silently changing strategy geometry.

The execution record stores both canonical and broker levels, the Bid/Ask and
spread at submission, and `BROKER_AWARE_BID_REFERENCE_V1`.

Submission-time adjustment is only an approximation because spread changes after
entry. Trade Manager must continue to monitor Bid/Ask and use broker close facts
as authoritative. Broker-native TP/SL remains the disconnect protection layer.

Canonical/theoretical outcomes disclaimer:

> Signal outcomes are evaluated using StratRelay reference market data and may
> differ from live broker execution because of spread, slippage, commissions,
> latency, and broker-specific pricing.
