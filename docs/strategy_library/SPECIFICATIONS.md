# Frozen experimental definitions — library V1

Generated from catalog.py. The accompanying catalog.json contains every resolved parameter and exact artifact identity. Shared temporal/entry/outcome rules are in README.md. These are research specifications, not performance claims.

## ICT

### ICT 2022 Model V1

Identity: `ICT_2022_MODEL@V1`. Mechanism: `ict_fvg`.

H1 structural bias; prior-channel liquidity sweep/reclaim; displacement and micro break; FVG forms then later retraces with directional rejection.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "2022_model",
  "family": "ICT",
  "name": "ICT 2022 Model",
  "mode": "ict_fvg",
  "hypothesis": "H1 structural bias; prior-channel liquidity sweep/reclaim; displacement and micro break; FVG forms then later retraces with directional rejection.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Silver Bullet V1

Identity: `ICT_SILVER_BULLET@V1`. Mechanism: `ict_fvg`.

ICT sweep/displacement/FVG composition restricted to 10:00–11:00 America/New_York; confirmation must complete strictly before window end.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "silver_bullet",
  "family": "ICT",
  "name": "Silver Bullet",
  "mode": "ict_fvg",
  "hypothesis": "ICT sweep/displacement/FVG composition restricted to 10:00\u201311:00 America/New_York; confirmation must complete strictly before window end.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "America/New_York",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 600,
  "trade_end": 660,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Judas Swing V1

Identity: `ICT_JUDAS_SWING@V1`. Mechanism: `range_fade`.

Complete 00:00–05:00 London range; 07:00–10:00 false break and close back inside; reject in reclaim direction; target opposite range edge.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: OPPOSITE_RANGE.

Resolved experimental parameters:

```json
{
  "key": "judas_swing",
  "family": "ICT",
  "name": "Judas Swing",
  "mode": "range_fade",
  "hypothesis": "Complete 00:00\u201305:00 London range; 07:00\u201310:00 false break and close back inside; reject in reclaim direction; target opposite range edge.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Europe/London",
  "range_start": 0,
  "range_end": 300,
  "trade_start": 420,
  "trade_end": 600,
  "symbols": [],
  "target_policy": "OPPOSITE_RANGE",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### OTE Retracement V1

Identity: `ICT_OTE_RETRACEMENT@V1`. Mechanism: `ote`.

H1 confirmed trend and latest confirmed impulse; enter on rejection of its 62–79 percent retracement after anchors become knowable.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ote_retracement",
  "family": "ICT",
  "name": "OTE Retracement",
  "mode": "ote",
  "hypothesis": "H1 confirmed trend and latest confirmed impulse; enter on rejection of its 62\u201379 percent retracement after anchors become knowable.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Killzone Liquidity Sweep V1

Identity: `ICT_KILLZONE_LIQUIDITY_SWEEP@V1`. Mechanism: `sweep`.

Prior-channel sweep and same-candle reclaim with rejection during 07:00–10:00 Europe/London.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "killzone_liquidity_sweep",
  "family": "ICT",
  "name": "Killzone Liquidity Sweep",
  "mode": "sweep",
  "hypothesis": "Prior-channel sweep and same-candle reclaim with rejection during 07:00\u201310:00 Europe/London.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Europe/London",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 420,
  "trade_end": 600,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## SMC

### BOS Retest V1

Identity: `SMC_BOS_RETEST@V1`. Mechanism: `bos_retest`.

H1 confirmed trend; close beyond latest same-direction confirmed M15 swing; subsequent retest and directional confirmation.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "bos_retest",
  "family": "SMC",
  "name": "BOS Retest",
  "mode": "bos_retest",
  "hypothesis": "H1 confirmed trend; close beyond latest same-direction confirmed M15 swing; subsequent retest and directional confirmation.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### CHOCH Reversal V1

Identity: `SMC_CHOCH_REVERSAL@V1`. Mechanism: `choch`.

Established M15 confirmed structural trend; opposing close beyond latest confirmed swing; later retest and reversal confirmation.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "choch_reversal",
  "family": "SMC",
  "name": "CHOCH Reversal",
  "mode": "choch",
  "hypothesis": "Established M15 confirmed structural trend; opposing close beyond latest confirmed swing; later retest and reversal confirmation.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Order Block Retracement V1

Identity: `SMC_ORDER_BLOCK_RETRACEMENT@V1`. Mechanism: `order_block`.

Displacement closes beyond prior channel; last opposing candle within five bars defines a wick-range block; later rejection on its retest.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "order_block_retracement",
  "family": "SMC",
  "name": "Order Block Retracement",
  "mode": "order_block",
  "hypothesis": "Displacement closes beyond prior channel; last opposing candle within five bars defines a wick-range block; later rejection on its retest.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### FVG Retracement V1

Identity: `SMC_FVG_RETRACEMENT@V1`. Mechanism: `fvg`.

H1 structural bias and displacement; three-bar wick FVG becomes knowable at third close; later uninvalidated-zone rejection.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "fvg_retracement",
  "family": "SMC",
  "name": "FVG Retracement",
  "mode": "fvg",
  "hypothesis": "H1 structural bias and displacement; three-bar wick FVG becomes knowable at third close; later uninvalidated-zone rejection.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Liquidity Displacement Scalp — preserved existing definition

Identity: `LIQUIDITY_DISPLACEMENT_SCALP_V1@V1`. Existing source: `liquidity_displacement`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.

## Breakout

### Opening Range Break V1

Identity: `BREAKOUT_OPENING_RANGE_BREAK@V1`. Mechanism: `range_break`.

09:30–10:00 New York opening-range proxy; first completed close crossing outside during 10:00–12:00; fixed-R target. OTC/crypto session proxy, not official exchange open.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "opening_range_break",
  "family": "Breakout",
  "name": "Opening Range Break",
  "mode": "range_break",
  "hypothesis": "09:30\u201310:00 New York opening-range proxy; first completed close crossing outside during 10:00\u201312:00; fixed-R target. OTC/crypto session proxy, not official exchange open.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "America/New_York",
  "range_start": 570,
  "range_end": 600,
  "trade_start": 600,
  "trade_end": 720,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Tokyo Range Break V1

Identity: `BREAKOUT_TOKYO_RANGE_BREAK@V1`. Mechanism: `range_break`.

Complete 09:00–12:00 Asia/Tokyo range; first close crossing outside during 12:00–15:00 Tokyo.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "tokyo_range_break",
  "family": "Breakout",
  "name": "Tokyo Range Break",
  "mode": "range_break",
  "hypothesis": "Complete 09:00\u201312:00 Asia/Tokyo range; first close crossing outside during 12:00\u201315:00 Tokyo.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Asia/Tokyo",
  "range_start": 540,
  "range_end": 720,
  "trade_start": 720,
  "trade_end": 900,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### London Breakout V1

Identity: `BREAKOUT_LONDON_BREAKOUT@V1`. Mechanism: `range_break`.

Complete 00:00–07:00 London range; first close crossing outside during 08:00–11:00 London.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "london_breakout",
  "family": "Breakout",
  "name": "London Breakout",
  "mode": "range_break",
  "hypothesis": "Complete 00:00\u201307:00 London range; first close crossing outside during 08:00\u201311:00 London.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Europe/London",
  "range_start": 0,
  "range_end": 420,
  "trade_start": 480,
  "trade_end": 660,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### NY Open Breakout V1

Identity: `BREAKOUT_NY_OPEN_BREAKOUT@V1`. Mechanism: `range_break`.

Complete 08:00–09:30 New York pre-open range; first outside close during 09:30–11:00. Distinct pre-open hypothesis from Opening Range Break.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ny_open_breakout",
  "family": "Breakout",
  "name": "NY Open Breakout",
  "mode": "range_break",
  "hypothesis": "Complete 08:00\u201309:30 New York pre-open range; first outside close during 09:30\u201311:00. Distinct pre-open hypothesis from Opening Range Break.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "America/New_York",
  "range_start": 480,
  "range_end": 570,
  "trade_start": 570,
  "trade_end": 660,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Donchian Breakout V1

Identity: `BREAKOUT_DONCHIAN_BREAKOUT@V1`. Mechanism: `channel_break`.

Completed close crosses prior 20-bar channel; signal candle excluded from its own range.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "donchian_breakout",
  "family": "Breakout",
  "name": "Donchian Breakout",
  "mode": "channel_break",
  "hypothesis": "Completed close crosses prior 20-bar channel; signal candle excluded from its own range.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Mean Reversion

### EUR Mean Reversion V1

Identity: `MEAN_REVERSION_EUR_MEAN_REVERSION@V1`. Mechanism: `bollinger`.

EURUSD only: close re-enters prior 20-bar two-standard-deviation band after an outside close; target frozen mean.

Context/execution: H1/M15. Instrument scope: EURUSD. Target: MEAN.

Resolved experimental parameters:

```json
{
  "key": "eur_mean_reversion",
  "family": "Mean Reversion",
  "name": "EUR Mean Reversion",
  "mode": "bollinger",
  "hypothesis": "EURUSD only: close re-enters prior 20-bar two-standard-deviation band after an outside close; target frozen mean.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [
    "EURUSD"
  ],
  "target_policy": "MEAN",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Session VWAP Fade V1

Identity: `MEAN_REVERSION_SESSION_VWAP_FADE@V1`. Mechanism: `vwap_fade`.

UTC-day tick-volume VWAP from completed prior bars; prior close exceeds 1.5 ATR deviation then current closes inward; target frozen VWAP.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: VWAP.

Resolved experimental parameters:

```json
{
  "key": "session_vwap_fade",
  "family": "Mean Reversion",
  "name": "Session VWAP Fade",
  "mode": "vwap_fade",
  "hypothesis": "UTC-day tick-volume VWAP from completed prior bars; prior close exceeds 1.5 ATR deviation then current closes inward; target frozen VWAP.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "VWAP",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Bollinger Reversion V1

Identity: `MEAN_REVERSION_BOLLINGER_REVERSION@V1`. Mechanism: `bollinger`.

Close returns inside prior 20-bar two-standard-deviation envelope after an outside close; target frozen mean.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: MEAN.

Resolved experimental parameters:

```json
{
  "key": "bollinger_reversion",
  "family": "Mean Reversion",
  "name": "Bollinger Reversion",
  "mode": "bollinger",
  "hypothesis": "Close returns inside prior 20-bar two-standard-deviation envelope after an outside close; target frozen mean.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "MEAN",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### RSI Reversion V1

Identity: `MEAN_REVERSION_RSI_REVERSION@V1`. Mechanism: `rsi_reversion`.

Cutler RSI14 crosses back above 30/below 70; target prior 20-bar mean.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: MEAN.

Resolved experimental parameters:

```json
{
  "key": "rsi_reversion",
  "family": "Mean Reversion",
  "name": "RSI Reversion",
  "mode": "rsi_reversion",
  "hypothesis": "Cutler RSI14 crosses back above 30/below 70; target prior 20-bar mean.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "MEAN",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Session Reversal V1

Identity: `MEAN_REVERSION_SESSION_REVERSAL@V1`. Mechanism: `range_fade`.

Complete 00:00–06:00 UTC range; 06:00–10:00 false break with rejection back inside; target opposite edge.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: OPPOSITE_RANGE.

Resolved experimental parameters:

```json
{
  "key": "session_reversal",
  "family": "Mean Reversion",
  "name": "Session Reversal",
  "mode": "range_fade",
  "hypothesis": "Complete 00:00\u201306:00 UTC range; 06:00\u201310:00 false break with rejection back inside; target opposite edge.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 360,
  "trade_start": 360,
  "trade_end": 600,
  "symbols": [],
  "target_policy": "OPPOSITE_RANGE",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Trend Following

### EMA Pullback V1

Identity: `TREND_FOLLOWING_EMA_PULLBACK@V1`. Mechanism: `ema_pullback`.

EMA20 above/below EMA50 on completed prior M15; current candle touches EMA20 and confirms in trend direction.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ema_pullback",
  "family": "Trend Following",
  "name": "EMA Pullback",
  "mode": "ema_pullback",
  "hypothesis": "EMA20 above/below EMA50 on completed prior M15; current candle touches EMA20 and confirms in trend direction.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### EMA 20/50 Trend V1

Identity: `TREND_FOLLOWING_EMA_20_50@V1`. Mechanism: `ema_cross`.

EMA20 crosses EMA50 on completed M15 close; structural recent-extreme stop.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ema_20_50",
  "family": "Trend Following",
  "name": "EMA 20/50 Trend",
  "mode": "ema_cross",
  "hypothesis": "EMA20 crosses EMA50 on completed M15 close; structural recent-extreme stop.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### 50/200 Trend V1

Identity: `TREND_FOLLOWING_EMA_50_200@V1`. Mechanism: `ema_cross`.

EMA50 crosses EMA200 on completed M15 close; structural recent-extreme stop.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ema_50_200",
  "family": "Trend Following",
  "name": "50/200 Trend",
  "mode": "ema_cross",
  "hypothesis": "EMA50 crosses EMA200 on completed M15 close; structural recent-extreme stop.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 50,
  "slow": 200,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Structure Pullback V1

Identity: `TREND_FOLLOWING_STRUCTURE_PULLBACK@V1`. Mechanism: `structure_pullback`.

H1 confirmed HH/HL or LH/LL; M15 retest of latest same-direction H1 swing with rejection.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "structure_pullback",
  "family": "Trend Following",
  "name": "Structure Pullback",
  "mode": "structure_pullback",
  "hypothesis": "H1 confirmed HH/HL or LH/LL; M15 retest of latest same-direction H1 swing with rejection.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Trendline Break + Retest V1

Identity: `TREND_FOLLOWING_TRENDLINE_BREAK_RETEST@V1`. Mechanism: `trendline_retest`.

Two confirmed M15 lows/highs form an existing sloping line; close breaks it; subsequent retest confirms in break direction.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "trendline_break_retest",
  "family": "Trend Following",
  "name": "Trendline Break + Retest",
  "mode": "trendline_retest",
  "hypothesis": "Two confirmed M15 lows/highs form an existing sloping line; close breaks it; subsequent retest confirms in break direction.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Momentum

### BTC Momentum Flow V1

Identity: `MOMENTUM_BTC_MOMENTUM_FLOW@V1`. Mechanism: `momentum`.

BTCUSD only: prior EMA20/50 alignment and close beyond prior 20-bar range with 1.5 ATR directional body.

Context/execution: H1/M15. Instrument scope: BTCUSD. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "btc_momentum_flow",
  "family": "Momentum",
  "name": "BTC Momentum Flow",
  "mode": "momentum",
  "hypothesis": "BTCUSD only: prior EMA20/50 alignment and close beyond prior 20-bar range with 1.5 ATR directional body.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.5,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [
    "BTCUSD"
  ],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### ATR Expansion V1

Identity: `MOMENTUM_ATR_EXPANSION@V1`. Mechanism: `expansion`.

Current range >=2 prior ATR and directional body >=65 percent range; close breaks prior five-bar extreme.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "atr_expansion",
  "family": "Momentum",
  "name": "ATR Expansion",
  "mode": "expansion",
  "hypothesis": "Current range >=2 prior ATR and directional body >=65 percent range; close breaks prior five-bar extreme.",
  "context": "H1",
  "execution": "M15",
  "lookback": 5,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 2.0,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Breakout Momentum V1

Identity: `MOMENTUM_BREAKOUT_MOMENTUM@V1`. Mechanism: `momentum`.

Prior EMA20/50 alignment; close breaks prior 20-bar range with >=1.2 prior ATR directional body.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "breakout_momentum",
  "family": "Momentum",
  "name": "Breakout Momentum",
  "mode": "momentum",
  "hypothesis": "Prior EMA20/50 alignment; close breaks prior 20-bar range with >=1.2 prior ATR directional body.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### EMA Momentum V1

Identity: `MOMENTUM_EMA_MOMENTUM@V1`. Mechanism: `ema_momentum`.

EMA20/50 direction agrees with three strictly directional closes; current directional body crosses above 1.2 prior ATR.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ema_momentum",
  "family": "Momentum",
  "name": "EMA Momentum",
  "mode": "ema_momentum",
  "hypothesis": "EMA20/50 direction agrees with three strictly directional closes; current directional body crosses above 1.2 prior ATR.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Volatility Continuation V1

Identity: `MOMENTUM_VOLATILITY_CONTINUATION@V1`. Mechanism: `volatility_continuation`.

Prior displacement creates a range; within eight bars a later close breaks its extreme in the same direction.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "volatility_continuation",
  "family": "Momentum",
  "name": "Volatility Continuation",
  "mode": "volatility_continuation",
  "hypothesis": "Prior displacement creates a range; within eight bars a later close breaks its extreme in the same direction.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Liquidity

### Equal High/Low Sweep V1

Identity: `LIQUIDITY_EQUAL_LEVEL_SWEEP@V1`. Mechanism: `equal_sweep`.

Latest two confirmed M15 highs/lows within 0.15 prior ATR form liquidity; wick sweeps their outer extreme then closes inside with rejection.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "equal_level_sweep",
  "family": "Liquidity",
  "name": "Equal High/Low Sweep",
  "mode": "equal_sweep",
  "hypothesis": "Latest two confirmed M15 highs/lows within 0.15 prior ATR form liquidity; wick sweeps their outer extreme then closes inside with rejection.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Previous Day High/Low Sweep V1

Identity: `LIQUIDITY_PREVIOUS_DAY_SWEEP@V1`. Mechanism: `day_sweep`.

Complete prior UTC day high/low; sweep and close back inside with directional rejection.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "previous_day_sweep",
  "family": "Liquidity",
  "name": "Previous Day High/Low Sweep",
  "mode": "day_sweep",
  "hypothesis": "Complete prior UTC day high/low; sweep and close back inside with directional rejection.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Previous Week High/Low Sweep V1

Identity: `LIQUIDITY_PREVIOUS_WEEK_SWEEP@V1`. Mechanism: `week_sweep`.

Complete prior UTC Monday–Monday range; sweep/reclaim with rejection. Missing bars fail closed; no inferred holiday calendar.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "previous_week_sweep",
  "family": "Liquidity",
  "name": "Previous Week High/Low Sweep",
  "mode": "week_sweep",
  "hypothesis": "Complete prior UTC Monday\u2013Monday range; sweep/reclaim with rejection. Missing bars fail closed; no inferred holiday calendar.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Session Liquidity Reclaim V1

Identity: `LIQUIDITY_SESSION_LIQUIDITY_RECLAIM@V1`. Mechanism: `range_fade`.

00:00–06:00 London range; 07:00–11:00 sweep/reclaim with rejection; target opposite edge.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: OPPOSITE_RANGE.

Resolved experimental parameters:

```json
{
  "key": "session_liquidity_reclaim",
  "family": "Liquidity",
  "name": "Session Liquidity Reclaim",
  "mode": "range_fade",
  "hypothesis": "00:00\u201306:00 London range; 07:00\u201311:00 sweep/reclaim with rejection; target opposite edge.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Europe/London",
  "range_start": 0,
  "range_end": 360,
  "trade_start": 420,
  "trade_end": 660,
  "symbols": [],
  "target_policy": "OPPOSITE_RANGE",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Liquidity Run Continuation V1

Identity: `LIQUIDITY_LIQUIDITY_RUN_CONTINUATION@V1`. Mechanism: `liquidity_run`.

Sweep/reclaim in H1 trend direction; displacement and local 5-bar close break within eight bars; next-open continuation.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "liquidity_run_continuation",
  "family": "Liquidity",
  "name": "Liquidity Run Continuation",
  "mode": "liquidity_run",
  "hypothesis": "Sweep/reclaim in H1 trend direction; displacement and local 5-bar close break within eight bars; next-open continuation.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Session-Based

### Asian Range Fade V1

Identity: `SESSION-BASED_ASIAN_RANGE_FADE@V1`. Mechanism: `range_fade`.

09:00–12:00 Tokyo range; 12:00–15:00 sweep/reclaim rejection; opposite edge target.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: OPPOSITE_RANGE.

Resolved experimental parameters:

```json
{
  "key": "asian_range_fade",
  "family": "Session-Based",
  "name": "Asian Range Fade",
  "mode": "range_fade",
  "hypothesis": "09:00\u201312:00 Tokyo range; 12:00\u201315:00 sweep/reclaim rejection; opposite edge target.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Asia/Tokyo",
  "range_start": 540,
  "range_end": 720,
  "trade_start": 720,
  "trade_end": 900,
  "symbols": [],
  "target_policy": "OPPOSITE_RANGE",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### London Open Drive V1

Identity: `SESSION-BASED_LONDON_OPEN_DRIVE@V1`. Mechanism: `session_drive`.

08:00–09:00 London: >=1.2 ATR displacement breaks prior 20-bar channel; one episode per direction/session.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "london_open_drive",
  "family": "Session-Based",
  "name": "London Open Drive",
  "mode": "session_drive",
  "hypothesis": "08:00\u201309:00 London: >=1.2 ATR displacement breaks prior 20-bar channel; one episode per direction/session.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "Europe/London",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 480,
  "trade_end": 540,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### New York Overlap Continuation V1

Identity: `SESSION-BASED_NY_OVERLAP_CONTINUATION@V1`. Mechanism: `session_pullback`.

08:00–11:00 New York: EMA20/50 trend pullback and rejection, one episode per direction/session.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "ny_overlap_continuation",
  "family": "Session-Based",
  "name": "New York Overlap Continuation",
  "mode": "session_pullback",
  "hypothesis": "08:00\u201311:00 New York: EMA20/50 trend pullback and rejection, one episode per direction/session.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "America/New_York",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 480,
  "trade_end": 660,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### UTC VWAP Reclaim V1

Identity: `SESSION-BASED_UTC_VWAP_RECLAIM@V1`. Mechanism: `vwap_reclaim`.

Current close crosses frozen prior UTC-day tick-volume VWAP in H1 trend direction.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "utc_vwap_reclaim",
  "family": "Session-Based",
  "name": "UTC VWAP Reclaim",
  "mode": "vwap_reclaim",
  "hypothesis": "Current close crosses frozen prior UTC-day tick-volume VWAP in H1 trend direction.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Opening Range Retest V1

Identity: `SESSION-BASED_OPENING_RANGE_RETEST@V1`. Mechanism: `range_retest`.

09:30–10:00 New York range freezes; outside close followed by later boundary retest with confirmation before 12:00.

Context/execution: H1/M15. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "opening_range_retest",
  "family": "Session-Based",
  "name": "Opening Range Retest",
  "mode": "range_retest",
  "hypothesis": "09:30\u201310:00 New York range freezes; outside close followed by later boundary retest with confirmation before 12:00.",
  "context": "H1",
  "execution": "M15",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "America/New_York",
  "range_start": 570,
  "range_end": 600,
  "trade_start": 600,
  "trade_end": 720,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Swing Position

### H4 Structure Pullback V1

Identity: `SWING_POSITION_H4_STRUCTURE_PULLBACK@V1`. Mechanism: `structure_pullback`.

Completed H4 HH/HL or LH/LL; H1 pullback to latest directional H4 structural pivot with rejection.

Context/execution: H4/H1. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "h4_structure_pullback",
  "family": "Swing Position",
  "name": "H4 Structure Pullback",
  "mode": "structure_pullback",
  "hypothesis": "Completed H4 HH/HL or LH/LL; H1 pullback to latest directional H4 structural pivot with rejection.",
  "context": "H4",
  "execution": "H1",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Daily Channel Breakout V1

Identity: `SWING_POSITION_DAILY_CHANNEL_BREAKOUT@V1`. Mechanism: `channel_break`.

Completed D1 close outside prior 20-day channel; next daily open reference entry.

Context/execution: D1/D1. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "daily_channel_breakout",
  "family": "Swing Position",
  "name": "Daily Channel Breakout",
  "mode": "channel_break",
  "hypothesis": "Completed D1 close outside prior 20-day channel; next daily open reference entry.",
  "context": "D1",
  "execution": "D1",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Daily EMA Pullback V1

Identity: `SWING_POSITION_DAILY_EMA_PULLBACK@V1`. Mechanism: `ema_pullback`.

Daily EMA20/50 trend; daily EMA20 pullback and confirmation; next daily open.

Context/execution: D1/D1. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "daily_ema_pullback",
  "family": "Swing Position",
  "name": "Daily EMA Pullback",
  "mode": "ema_pullback",
  "hypothesis": "Daily EMA20/50 trend; daily EMA20 pullback and confirmation; next daily open.",
  "context": "D1",
  "execution": "D1",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### Weekly Range Break + Retest V1

Identity: `SWING_POSITION_WEEKLY_RANGE_BREAK_RETEST@V1`. Mechanism: `weekly_retest`.

Complete prior UTC Monday–Monday H4 range; H4 close outside followed by later retest within eight H4 bars.

Context/execution: D1/H4. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "weekly_range_break_retest",
  "family": "Swing Position",
  "name": "Weekly Range Break + Retest",
  "mode": "weekly_retest",
  "hypothesis": "Complete prior UTC Monday\u2013Monday H4 range; H4 close outside followed by later retest within eight H4 bars.",
  "context": "D1",
  "execution": "H4",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

### H4 Momentum Continuation V1

Identity: `SWING_POSITION_H4_MOMENTUM_CONTINUATION@V1`. Mechanism: `momentum`.

H4 EMA20/50 alignment and displacement close beyond prior 20-bar H4 channel.

Context/execution: D1/H4. Instrument scope: canonical instruments with audited data/metadata. Target: FIXED_R.

Resolved experimental parameters:

```json
{
  "key": "h4_momentum_continuation",
  "family": "Swing Position",
  "name": "H4 Momentum Continuation",
  "mode": "momentum",
  "hypothesis": "H4 EMA20/50 alignment and displacement close beyond prior 20-bar H4 channel.",
  "context": "D1",
  "execution": "H4",
  "lookback": 20,
  "fast": 20,
  "slow": 50,
  "atr_period": 14,
  "atr_multiple": 1.2,
  "tolerance_atr": 0.15,
  "buffer_atr": 0.1,
  "min_r": 1.0,
  "target_r": 2.0,
  "setup_bars": 8,
  "session_tz": "UTC",
  "range_start": 0,
  "range_end": 60,
  "trade_start": 60,
  "trade_end": 240,
  "symbols": [],
  "target_policy": "FIXED_R",
  "entry_policy": "NEXT_OPEN_RECONSTRUCTED",
  "max_entry_gap_atr": 0.25,
  "lifecycle": "RESEARCH",
  "execution_eligible": false,
  "version": "V1"
}
```

## Price Action

### Support/Resistance Retest — preserved existing definition

Identity: `PRICE_ACTION_SUPPORT_RESISTANCE_RETEST@V1`. Existing source: `strategy_backtest.price_action_sr_retest`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.

### Engulfing Continuation — preserved existing definition

Identity: `PRICE_ACTION_ENGULFING_CONTINUATION@V1`. Existing source: `strategy_backtest.price_action_engulfing_continuation`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.

### Engulfing Reversal — preserved existing definition

Identity: `PRICE_ACTION_ENGULFING_REVERSAL@V1`. Existing source: `strategy_backtest.price_action_engulfing_reversal`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.

### Morning/Evening Star Reversal — preserved existing definition

Identity: `PRICE_ACTION_STAR_REVERSAL@V1`. Existing source: `strategy_backtest.price_action_star_reversal`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.

### Rejection / Pin-Bar Reversal — preserved existing definition

Identity: `PRICE_ACTION_PIN_BAR_REVERSAL@V1`. Existing source: `strategy_backtest.price_action_pin_bar_reversal`. Original specification, parameters, historical evidence and behavior remain authoritative. No alternate implementation is introduced.
