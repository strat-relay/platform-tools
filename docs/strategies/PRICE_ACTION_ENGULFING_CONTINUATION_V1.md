# Price Action — Engulfing Continuation V1

Status: `RESEARCH` · execution eligible: `false` · family: `Price Action`.

This is a frozen experimental specification. Parameter values are explicit research
inputs and are not validated defaults.

## Causal definition

The context role is H1 and the execution role is M15. Only completed candles are
accepted. Strict H1 swings use two candles on each side and become available at the
close of the second right-hand candle. Trend is `UP` only when the latest two
confirmed resistance swings and latest two confirmed support swings both rise;
`DOWN` requires both pairs to fall. Mixed or insufficient structure is no trend.

The structural zone is the latest confirmed support for an uptrend or resistance for
a downtrend, clustered within eight ticks, and no older than 120 H1 bars. A completed
execution candle qualifies as the first pullback interaction when its low (uptrend)
or high (downtrend) enters that zone. The setup window is six execution bars from
that interaction. A same-candle interaction and confirmation is disallowed.

Confirmation is strict body engulfing: the prior candle must have a non-doji body in
the opposite direction; the confirmation candle must have a body in the trend
direction, open at or beyond the prior close, and close at or beyond the prior open.
Wick-only overlap, doji predecessors, equality in the body-direction tests, and a
confirmation body smaller than four ticks do not qualify. The first qualifying
confirmation creates one deduplicated episode.

The next M15 open is the only research entry observation. A gap beyond the zone by
more than eight ticks, invalid stop/entry/target geometry, or a trend invalidation
before entry cancels the proposal. The structural stop is the pullback extreme plus
two ticks. The target is the nearest opposing confirmed H1 level known at the
confirmation cutoff. Planned R must be at least 1.5 at confirmation and is checked
again using the actual research entry open. No executable signal or outcome is
created.

HTF invalidation is fail-closed: for an uptrend, a completed H1 close below the
latest confirmed support zone beyond tolerance invalidates a pending proposal; for a
downtrend, a close above resistance does so. Missing structure produces a rejection,
invalid OHLC raises a deterministic input error, and out-of-order data is rejected.
No wall clock, broker, network, or future candle fields are consulted at the next
open decision.

## Parameter/provenance manifest

The complete experimental parameter object, parameter hash, primitive implementation
identities, dependency hash, evaluator version, data snapshot, cutoff, episode id,
stage evidence, and rejection reason are recorded on every canonical Evaluation.
Shared primitive identities are in `strategy_backtest/price_action_primitives.py`.

Historical validation is blocked unless a suitable, audited H1/M15 dataset is
authorized and available. Synthetic replay is causality evidence only.

