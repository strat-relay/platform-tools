# 02 - What an "observation" is (canonical definition)

Design only. Grounded in what the current Trade Manager code **actually consumes** (`01`), not in what the Phase 7 record happens to contain.

## 1. Definition

> An **observation** is an immutable, time-stamped statement of *what the world looked like* for a given ManagedTrade at a given moment, sufficient - together with the ManagedTrade's own persisted state - for a frozen TradeManagerVersion to compute a decision deterministically. It contains **facts about the market**, identifies **which trade and version** they are for, and **never** contains the trade's derived state, an account, or a broker position.

Rules:

1. **Facts, not conclusions.** `current_R`, MFE/MAE, EMA, structure, management state are *derived* by the evaluator from facts plus persisted trade state. They are recorded as decision evidence, not carried in the observation. (Today's `CausalObserver` mixes both in one dict; the canonical contract separates them so that a different TradeManagerVersion can derive different things from the same observation.)
2. **As-of.** An observation carries `observed_at` (when the quote was taken) and `effective_at` (the as-of instant for bar completeness). Only bars closed at or before `effective_at` count as complete (existing `causal_candles` rule).
3. **Broker-independent.** Prices come from a `MarketDataProvider`; no account, ticket, lot size, or account mode (A2 doc `02` section 5).
4. **Sequenced per trade.** Each ManagedTrade has a gapless `observation_seq`, so loss, duplication and reordering are detectable without a checkpoint file.

## 2. Observation types

| Type | Scope | Owner | Needed by the current Trade Manager? | In the canonical product observation? |
|---|---|---|---|---|
| **MARKET observation** (`MarketQuote`) | instrument + provider + instant: `bid`, `ask`, `spread`, `source_timestamp`, `provider_id`, `feed_id`, price-semantics version | MarketDataProvider adapter | **Yes** - `bid`/`ask` (close-side executable price), spread | **Yes** |
| **BAR reference** (`bars_ref`) | instrument + timeframe + window | MarketDataProvider (bar store) | **Yes** - completed **M5** bars (EMA200 needs >= 200; structure uses swings on M5). **M1 is built but no decision path consumes it (`M12`)**; ATR is never produced | **Yes**, M5 only (M1 optional, off by default) |
| **TRADE observation** (this document's canonical event) | one ManagedTrade at one instant | Trade Management (Trading Core) | the *envelope* the evaluator consumes | **Yes** (it is the event) |
| TRADE state | ManagedTrade | Trade Management | current_R, MFE/MAE, current stop, management state, elapsed time | **No** - derived and persisted by the evaluator |
| **BROKER observation** (`PersonalPositionObservation`) | a broker position in one account | Personal Execution | **No** for policy. Used only by the Personal Execution translator (position exists, current SL/TP, volume, ticket) | **No** |
| **ACCOUNT observation** | one account | Personal Execution | **No** (`account_context_id`, `unrealized_pnl`, `position_size` are echoed by `TradeManager._record` but influence nothing) | **No** |
| **ANALYSIS observation** (`AnalysisProvider`, e.g. TradingView/Pine) | candidate / analysis event | AnalysisProvider | **No**; never an executable price | **No** (separate, non-executable type) |

Verification of "genuinely needed": `TradeManager.evaluate` requires `strategy_id, setup_id, economic_position_id, symbol, direction, entry, original_stop, original_target, size` from the *position* and `current_price` (+ optional `ema_200`, `stale`, `age_ms`) from the market. `size` is required but only echoed; `account_context_id`, `unrealized_pnl` are pass-through; nothing else account- or broker-related enters any comparison (`engine.py`, `phase2.py`, `trailing.py`, `observation.py`).

## 3. Canonical fields (summary; the full event contract is in `08`)

```
TradeObservation
  observation_id        stable id over (managed_trade_id, provider_id, instrument, source_timestamp, quote hash)
  observation_seq       per-ManagedTrade, gapless, assigned by the producer inside its transaction
  managed_trade_id, stream_id, strategy_version_id, trade_manager_version_id     # identity of the trade and the frozen versions it is bound to
  instrument (canonical), direction
  observed_at, effective_at
  quote { bid, ask, spread, source_timestamp, provider_id, feed_id, price_semantics_version }
  bars_ref { timeframe: M5, completed_through, count, digest }
  provenance { producer_id, producer_version, provider_capabilities, data_status }   # FORWARD | LIVE | REPLAY | BACKTEST
```

What is deliberately **absent**: bids/asks for M1, full bar arrays (referenced by digest; see `08`), account fields, broker tickets, lot sizes, R-multiples, MFE/MAE, EMA values, decisions.

## 4. Data-status and evidence class

Every observation carries the data status of the run that produced it: `FORWARD` (live market, frozen versions), `LIVE` (personal execution context; not the product observation), `REPLAY`/`BACKTEST` (historical provider). A decision inherits the status of the observation it used (A2 `06` provenance rule). This is how the same evaluator serves research, forward evidence and replay without a code path per mode.

## 5. What the Phase 7 record is, in these terms

The current `MARKET_OBSERVATION` is a *position-scoped bundle* (quote missing, 205 bars inline, no trade identity beyond a paper economic id, no versions, sequence assigned by a file appender). It is useful as evidence of what the evaluator needed - a quote and completed M5 history - and is **not** the canonical schema.
