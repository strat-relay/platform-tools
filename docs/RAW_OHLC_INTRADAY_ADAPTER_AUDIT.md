# Raw-OHLC intraday adapter audit

## Parent semantic mapping

### Context Structure Retrace

| Semantic | Parent implementation | Parent inputs | Intraday mapping | Classification |
|---|---|---|---|---|
| Directional context | `context_structure_retrace_forward._context` and `context_structure_retrace.replay.feature_snapshot` | completed H1/H4 | completed H1/H4 from one causal M5 aggregation | THESIS_OWNED + TIMEFRAME_DERIVED |
| Structure | `context_structure_retrace.sr.sr_context`, `trendline_candidates`, `channel_candidates` | completed execution-timeframe bars plus confirmed swings | completed M15 structural view derived from causal M5 aggregation | TIMEFRAME_DERIVED; parent parity fixture still required |
| Retracement | `context_structure_retrace_forward.make_setup`, `_process_bar` | setup candle and completed lower-timeframe bars | H1 setup, M15 confirmation/entry; parent depth semantics preserved | THESIS_OWNED + TIMEFRAME_DERIVED |
| Confirmation | `context_structure_retrace.patterns.detect_patterns`, `_m5_mechanisms` | completed M15/M5 candles | completed M15 pattern evidence | THESIS_OWNED + TIMEFRAME_DERIVED |
| Invalidation | `context_structure_retrace_forward._process_bar` | subsequent completed bars | same causal state machine | THESIS_OWNED |
| Re-entry | `context_structure_retrace_forward._process_bar` | leave-zone, return, target state | adapter state is restorable; full parent parity remains a gate | THESIS_OWNED |
| Entry economics | `make_setup`, `_fill` | setup bar, quote/contract, snapshot | parent-derived entry and generic `EntrySignal` | THESIS_OWNED |
| Stop/target | `_geometry` | setup extreme, ATR/spread, opposing structure | parent structural geometry; no new fixed R | THESIS_OWNED |
| Outcome | `_evaluate_open_position`, generic `SimulatedExecution` | completed post-entry bars | generic execution expiry uses parameterized 1440 minutes | TIMEFRAME_DERIVED / RESEARCH_HYPOTHESIS |

### Liquidity Displacement

| Semantic | Parent implementation | Parent inputs | Intraday mapping | Classification |
|---|---|---|---|---|
| Liquidity level | `LiquidityDisplacementStrategy._levels` | prior completed M5 swings/session extremes | prior completed M15/H1 context supplied to unchanged M5 predicate | THESIS_OWNED + TIMEFRAME_DERIVED |
| Sweep/reclaim | `find_candidate` | completed M5 candle and prior levels | M15 setup scan, no caller labels | THESIS_OWNED |
| Displacement | `find_candidate` | ATR, body, close location, completed forward prefix | unchanged parent predicate at causal current prefix | THESIS_OWNED |
| MSS/BOS | `find_candidate` | prior micro level and completed displacement candle | unchanged parent predicate | THESIS_OWNED |
| Retracement entry | `evaluate`, `_entry_for_fraction` | completed M5 bars after displacement | M5 execution refinement | THESIS_OWNED |
| Stop | `find_candidate` | sweep extreme, ATR, spread, broker minimum | unchanged parent stop; requires contract metadata | THESIS_OWNED + OPERATIONAL |
| Target | parent `evaluate` uses fixed 1.25R | parent parameter | intraday uses opposing structural level hypothesis; 1.25R excluded | SCALP_SPECIFIC removed; RESEARCH_HYPOTHESIS |
| Expiry | `max_retrace_candles`, parent `max_hold_minutes` | parent config | 3 retracement candles preserved; 1440-minute hold parameterized | TIMEFRAME_DERIVED + RESEARCH_HYPOTHESIS |
| Invalidation/outcome | parent forward/lifecycle code | completed M5 state | restorable adapter state plus generic outcome engine | THESIS_OWNED / ADAPTER GAP |

## Raw-OHLC boundary

The normal registry now resolves `ContextRawOhlcEvaluator` and
`LiquidityRawOhlcEvaluator`. They consume completed M5 `MarketEvent` values,
aggregate UTC M15/H1/H4 candles, and derive parent patterns/levels internally.
Caller-provided labels such as `is_sweep` or `pattern` are not required.

The earlier stage-evidence evaluator remains only as a synthetic unit-test seam.

Liquidity sweep indices are not marked resolved until the parent predicate's
five-candle forward displacement/MSS observation window is complete. This
prevents a live prefix from losing a valid candidate merely because the first
scan occurred before its required completed bars arrived.

## Remaining blockers

1. The unchanged Liquidity predicate requires positive spread and contract
   metadata. The local historical inventory contains pure OHLC only, so the
   adapter records a metadata block instead of changing the predicate.
2. There is no frozen equivalent parent Context/Liquidity raw fixture in the
   repository that can prove full setup/entry/stop/target/outcome parity.
3. Only H1 XAUUSD datasets exist locally; no required M5/M15 coverage exists
   for any requested discovery instrument.

Therefore this commit makes the raw path mechanically invocable but does not
declare discovery readiness.

```text
DISCOVERY_ACCESSED=false
UNTOUCHED_VALIDATION_ACCESSED=false
PARAMETER_OPTIMIZATION=false
PARENT_STRATEGIES_CHANGED=false
EXISTING_INSTANCES_CHANGED=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0
```
