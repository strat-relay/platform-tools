# 05 - MarketDataProvider boundary for the observation producer

A2 (`ADR-0001` D7, `08`) established three ports: `MarketDataProvider`, `AnalysisProvider`, `BrokerClient`. This document fixes how the **Trade Observation Service (TOS)** and the Trade Manager relate to them. No code is specified beyond port shape.

## 1. Who uses which port

| Component | MarketDataProvider | AnalysisProvider | BrokerClient |
|---|---|---|---|
| Trade Observation Service (Trading Core) | **yes** (only data source) | no | **never** |
| Trade Manager evaluator | no (consumes observations; may resolve a `bars_ref` through the same port, read-only) | no | **never** |
| Strategy runners / StrategyHost | yes (their own inputs) | no | never |
| Studio / candidate detection | yes | **yes** | never |
| Personal Execution translator | no | no | **yes** (read for eligibility; write only in P5) |
| Personal Execution broker-state publisher | no | no | yes (read) |

The Trade Manager evaluator has **no import path to a broker or bridge client**: its only inputs are an observation event and the ManagedTrade row. This is already true of the pure modules (`engine.py`, `observation.py`: "This module never reads MT5", `01` section 4) and becomes an enforced package boundary.

## 2. Port shape required by the observation path

```
MarketDataProvider
  provider_id, capabilities: { EXECUTABLE_QUOTES: bool, BARS: [M5,...], SERVER_TIME: bool, HISTORICAL: bool }
  quote(instrument)                     -> MarketQuote { instrument, bid, ask, spread, source_timestamp, provider_id, feed_id }
  bars(instrument, timeframe, end, count)   -> BarWindow { bars[], completed_through, digest }     # completed bars only when end is an as-of
  instrument_spec(instrument)           -> tick size, digits, contract, session (no account data)
  historical(instrument, tf, start, end)    -> stream of BarWindow (REPLAY / BACKTEST)
```

* `instrument` is **canonical** (`XAUUSD`); the adapter maps to provider symbols (`XAUUSDm`), as A2 `ADR-0001` D7 requires. Today the mapping lives in several places (`canonical_symbol`, `broker_symbol_hint`, `rstrip("m")`).
* **`digest`** is a content hash of the completed-bar window; it pins what the evaluator saw (replay and audit) without inlining 205 bars in every event.
* A provider without `EXECUTABLE_QUOTES` cannot feed a `TradeObservation` quote.

## 3. The current implementation behind the port (adapter, not architecture)

| Port method | Today | Adapter notes |
|---|---|---|
| `quote` | `mt5_quote` (never called by Phase 7, `M1`) | `Mt5ReadClient.call("mt5_quote")` on the **research** listener |
| `bars` | `mt5_rates` (`limit` <= 500), last bar incomplete (`rates[:-1]` convention in runners; `causal_candles` in the TM) | adapter must drop the forming bar unless explicitly requested |
| `instrument_spec` | `mt5_symbol_info` | includes `point`/`tick_size` |
| `historical` | `mt5_rates_range` (M5/M15/H1/H4) | for replay |

`contracts.mt5_bridge.Mt5ReadClient` is the right base (read tools only; it refuses non-read tools). The adapter must use the **research listener**, not the execution bridge:

* the execution bridge serves one EA queue shared with order preflight and sends (A5 `01`, `B9`); observation reads would compete with the order path;
* the research listener exposes no write tools (A5 `B4`).

## 4. Feed identity is a first-class attribute

The strategy reference fill (and therefore the ManagedTrade geometry) is defined on a specific feed:

| Strategy | Data path today | Note |
|---|---|---|
| Context | bridge read tools (`call_bridge` on `--mcp-url`, default `http://127.0.0.1:22347/mcp`; the stack registry passes no override and separately defines a research bridge on :22350) | HTTP; which research listener is in use is runtime configuration (`19`) |
| Liquidity family | `read_once()` subprocess (`wine ... mt5_read_once.py`, A5 `08`) | MT5 wine prefix from environment; not the bridge |
| Personal execution | execution terminal / execution bridge | a **different terminal and broker session** |

Rules:

1. `MarketQuote.feed_id` and the `StreamBinding.market_feed_id` are recorded; the TOS observes **the feed the strategy's reference fill used**. Management decisions are levels on that reference trade.
2. A gap between the reference feed and the executing account's feed is a **Personal Execution translation concern** (A2 open decision O12, DEMO docs "cross-feed geometry"): the translator maps a reference *level* to the broker with its own quote and rejects or adjusts by explicit policy. The product observation never contains the execution feed.
3. If the reference feed changes for an open ManagedTrade (source switch), the trade **does not silently rebind**: see `18` (observation source switches during an open trade).

## 5. AnalysisProvider and TradingView

TradingView/Pine outputs are `AnalysisProvider` events: candidates, zones, indicators. They:

* may become **AnalysisObservation** records for research, Studio and `strategy.candidate.detected` events;
* **never** populate `TradeObservation.quote` and never set an executable price;
* may be referenced by evidence (`provenance`) but cannot substitute for a `MarketQuote`.

`BrokerClient` remains authoritative for execution price and order state; the product never derives an execution price from any provider.

## 6. Replay and determinism

`REPLAY`/`BACKTEST` use the same TOS derivation code against a `historical` provider, producing the same `TradeObservation` shape with `data_status = REPLAY`. The evaluator therefore has **one** code path across FORWARD, REPLAY and BACKTEST. The existing offline `replay.py` fixture (Phase 7 XAUUSD case) becomes the first golden corpus (`16`).

## 7. What the TOS must not know

Accounts, tickets, lots, execution mode, entitlement, subscriptions, customer identities, the bridge's internal lifecycle, or the personal broker's state.
