# SIMPLE_SR_CANDLE_V1

Status: independent historical research only; no forward runner.

## Design

This branch investigated simple candle patterns and horizontal structure on
XAUUSDm. Research modes include engulfings, morning/evening stars, M15
engulfings, support/resistance comparisons, continuation studies, and later
pattern-discovery studies. The branch was kept separate from liquidity
displacement.

## Historical commands

```sh
cd /Users/caleb/mt5-native-bridge
python3 simple_sr_candle_validate.py
SIMPLE_SR_PATTERN_MODE=M15_ENGULFING_ONLY python3 simple_sr_candle_validate.py
SIMPLE_SR_PATTERN_MODE=STARS_ONLY python3 simple_sr_candle_validate.py
```

Related descriptive studies:

```sh
python3 engulfing_continuation_study.py
python3 candle_sequence_discovery.py
python3 raw_sequence_discovery.py
python3 prior_rally_bullish_engulfing_study.py
```

These scripts generate research artifacts only. They do not create a paper
runner or submit orders.

## Constraints / future plans

Named candle patterns did not demonstrate a robust universal edge in the
completed research. Do not turn a visually convincing subgroup into a runner
without a new discovery/holdout design and explicit causal definitions.
