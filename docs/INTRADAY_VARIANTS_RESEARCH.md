# Research-only intraday variants

The definitions in `research/intraday_variants.py` are additive, offline
research catalog entries. They do not publish strategy rows, create PostgreSQL
instances, register runners, alter memberships, or change execution policy.

## Parent audit

### `CONTEXT_STRUCTURE_RETRACE_V1`

- Implementation: `context_structure_retrace_forward.py` and
  `context_structure_retrace/`.
- Context: H1/H4; execution/confirmation: M15; lower context: M5.
- Thesis: higher-timeframe context → structural retracement → lower-timeframe
  confirmation → entry.
- Stop: originating setup extreme with causal volatility/spread safety buffer.
- Target: structure-capped extension; no fixed target R in the frozen manifest.
- Max hold: none in the parent.
- Re-entry: leave zone, completed lower-timeframe close outside, return, thesis
  valid, target not completed.
- Parameter set: `context-v1-frozen`.
- Initial research membership: canonical `XAUUSD`, `BTCUSD`, `USDJPY`, and
  `EURUSD`, matching the parent’s available historical cohorts. Provider
  symbols remain outside the strategy definition.

### `LIQUIDITY_DISPLACEMENT_SCALP_V1`

- Implementation: `liquidity_displacement.py` and
  `orchestration/liquidity_live.py`.
- Context/setup: M15; confirmation/entry: M5.
- Thesis: liquidity sweep → reclaim → displacement → MSS → retracement entry.
- Stop: sweep extreme plus causal ATR/spread/broker minimum safety buffer.
- Target: fixed `1.25R` in the parent live adapter; this is treated as
  scalp-horizon parameterization and is not copied into the intraday variant.
- Max hold: `120` minutes in the parent.
- Re-entry: one frozen retracement opportunity per setup.
- Parameter sets: explicit instance sets such as `liquidity-v1-xau-50`,
  `liquidity-v1-btc-25`, and `liquidity-v1-usdjpy-25`.
- Initial research membership: canonical `XAUUSD`, `BTCUSD`, and `USDJPY`,
  limited to cohorts with existing historical data.

## New variants

| Variant | Hierarchy | Hold proposal | Target proposal | Instance |
|---|---|---:|---|---|
| `CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1` | H4 → H1 → M15 | 1440 minutes, UTC-day boundary | Research hypothesis: structural capped extension; no fixed R selected | `context-intraday-v1`, OFFLINE |
| `LIQUIDITY_DISPLACEMENT_INTRADAY_V1` | H1 → M15 → M5 | 1440 minutes, UTC-day boundary | Research hypothesis: opposing structural liquidity/swing; parent 1.25R not copied | `liquidity-intraday-v1`, OFFLINE |

Both variants have distinct strategy IDs, V1 identities, evaluator keys,
parameter schemas, parameter-set IDs, fingerprints, and instance IDs. All
parameters include provenance: parent-preserved, timeframe-derived,
research-hypothesis, or operational.

## Adapter boundary

The generic backtest core now has one deterministic UTC multi-timeframe
aggregator, a restorable completed-candle state, registered adapter keys, and
raw-OHLC adapters that call the parent candle-derived predicates. Historical
and live inputs use the same evaluator contract.

The remaining readiness gaps are data/verification gaps: Liquidity's unchanged
parent predicate requires spread/contract metadata, while the available pure
OHLC inventory does not carry it; and frozen equivalent parent fixtures are
still required for full parity. The earlier explicit-stage adapter remains
available only as a semantic unit-test seam; normal registry invocation now
uses the raw-OHLC adapters.

This is intentionally not a parameter search and not a production activation.
