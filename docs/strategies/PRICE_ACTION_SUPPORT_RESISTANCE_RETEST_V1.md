# Price Action — Support/Resistance Retest V1

Status: `RESEARCH` · execution eligible: `false` · family: `Price Action`.

This is a frozen experimental specification. The parameter values are reproducible
research inputs, not validated defaults or a production registration.

## Causal definition

The context timeframe is H1 and the execution timeframe is M15. Only completed
candles are consumed. A strict swing uses two candles on each side; a swing at
index `i` becomes available at the close of `i + 2`. A support/resistance level is
the mean price of same-kind confirmed swings within eight ticks, using only swings
known at the evaluation cutoff. A level expires after 120 context bars and only the
latest 80 context swings are eligible.

For each direction, the sequence is:

1. A completed M15 candle closes beyond a confirmed opposing level by the one-tick
   break buffer. The break candle is the anchor and cannot also be a retest.
2. One of the next four completed M15 candles interacts with the level and closes on
   the new side. The first subsequent candle cannot also confirm because the frozen
   experimental parameter `allow_retest_confirmation` is false.
3. The retest candle must be either a strict bullish/bearish rejection (same-colour
   body, wick/body at least 1.5, close-location at least 0.65) or a strict two-candle
   engulfing pattern. The candle's close is the reference entry price; the next M15
   open is the only eligible entry event. The evaluator does not expose that future
   open while deciding the setup.
4. The stop is the retest extreme plus two ticks. The target is the nearest known
   opposing H1 level on the profitable side. The proposal is rejected when geometry
   is invalid or planned R is below 1.5.

The next-open proposal is cancelled when the next open gaps through the level by
more than eight ticks, when the next open is unavailable, or when the candidate has
expired. This research foundation records the proposal and cancellation evidence;
it does not produce an executable signal or resolve outcomes.

Duplicate breaks select the latest break before a retest. A new break supersedes an
older open candidate for the same level/direction. Missing, stale, invalid, duplicate,
or out-of-order data fails closed: invalid OHLC raises a deterministic input error,
out-of-order events are rejected, and missing structure yields a `NO_CANDIDATE`
evaluation. No wall clock, network, broker write, mutable global, or future outcome
label is used.

## Frozen experimental parameters

The complete parameter object and its SHA-256 hash are emitted on every Evaluation.
See `strategy_backtest/price_action_sr_retest.py` (`RESEARCH_PARAMETER_VALUES`).
Primitive identities are pinned in `PRIMITIVE_MANIFEST`; semantic changes require a
new primitive identity and a new strategy version.

## Evidence boundary

The evaluator emits canonical `Evaluation` and `DecisionTrace` records with stage
evidence, cutoff, candidate identity, data snapshot identity, parameter hash,
dependency hash, and evaluator identity. `Decision.REJECT` with
`research_only=true` means “proposal observed but not an executable signal.”
No historical market dataset is bundled by this change, so historical validation is
blocked. Synthetic replay tests demonstrate causality and determinism only.

