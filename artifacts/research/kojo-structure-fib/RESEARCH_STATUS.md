# KOJO_STRUCTURE_FIB Research Status

## Status

`HISTORICAL_108_STUDY_REPRODUCIBLE=false`

The previous 108-cell study is closed as **NON-REPRODUCIBLE HISTORICAL
RESEARCH EVIDENCE**. Its reported `42 / 0 / 0` result is retained only as
historical diagnostic evidence and is not an acceptance gate for future work.

The original runner and original result artifact are unavailable. The study
must not be reconstructed from conversational summaries, and no replacement
parameter grid is authorized by this status.

## Source fixture

- `SOURCE_FIXTURE_RECOVERED=true`
- Provider: `OANDA`
- Symbol: `XAUUSD`
- Timeframe: `H1`
- Bars: `183`
- Canonical fixture:
  `source-fixture-recovery/canonical_bars.v1.json`
- SHA-256:
  `fbb5ec907c8f6bd979e49012eea0510ba06dcc50fa00916f8776500d91de8af9`
- Actual fixture range: `2025-12-03T00:00:00Z` through
  `2025-12-12T21:00:00Z`

## Source-explicit rules

The source material explicitly describes:

- XAUUSD
- H1
- a third touch of structure after two clean reactions
- retracement into the 61.8–78.6% Fibonacci pocket
- a timely H1 bullish engulfing candle confirming intent
- no market-moving fundamentals on the calendar

These statements are source evidence, but they do not fully specify a
deterministic evaluator.

## Fixture observations

The recovered Dec 11, 2025 OANDA fixture records:

- approximate source entry: `4211.932`
- approximate source stop: `4201.624`
- approximate source target: `4250.102`
- chart structure visually around `4200`
- canonical fixture fingerprint:
  `fbb5ec907c8f6bd979e49012eea0510ba06dcc50fa00916f8776500d91de8af9`

The approximate levels are observations only. They are not evaluator rules.

## Research hypotheses, not source rules

The lost study reportedly explored:

- structure families A, B, and C
- reaction separation of 2 or 3 bars
- wick or body touch
- three Fibonacci anchor algorithms
- three engulfing definitions
- a `0.005` feed tolerance

These remain historical research hypotheses. They are not promoted to Kojo
source rules.

## Lost research evidence

The unavailable study reported:

```text
DEC10_13_MATCHES=42
DEC11_06_MATCHES=0
DEC11_12_MATCHES=0
```

It also had a known implementation defect: third-touch qualification used
`visit_count >= 3` rather than literal visit ordinal 3. Therefore:

```text
HISTORICAL_42_0_0_AUTHORITATIVE=false
```

The historical result is preserved as a reported observation, not reproduced
or independently validated.

## Deterministic rules currently supported

Only the following are currently supported without inventing semantics:

- canonical identity: `XAUUSD`
- provider: `OANDA`
- timeframe: `H1`
- completed-bar source fixture with 183 normalized bars
- literal source phrase requiring a third touch after two clean reactions
- Fibonacci pocket stated as 61.8–78.6%
- H1 bullish engulfing confirmation as a qualitative source concept
- exclusion of market-moving fundamentals as a qualitative source concept

These are insufficient to implement a source-faithful detector.

## Unresolved source semantics

The minimum unresolved semantics are:

### Structure

- mechanical definition of a structure level or zone
- price tolerance and whether tolerance is absolute, relative, or volatility-scaled
- whether the structure is support, resistance, or either

### Clean reactions

- what makes reaction #1 and reaction #2 clean
- required departure magnitude, duration, and direction
- whether a reaction is wick-based, body-based, or range-based

### Distinct touch and departure

- what constitutes one distinct touch/visit
- whether consecutive bars at the level are one visit or multiple visits
- what price/time movement constitutes meaningful departure between visits
- whether a touch can be invalidated by penetration or close through the level

### Structure lifecycle

- when a structure is created
- when touch counting begins
- expiry conditions
- whether and when a structure may reset or be reborn
- whether later tests belong to the same lifecycle

### Fibonacci

- exact impulse/swing supplying the 0% and 100% anchors
- confirmation timing for each anchor
- treatment of overlapping or competing swings
- whether the pocket is measured from the retracement high or low

### Engulfing and timing

- exact body/wick engulfing definition
- whether equality counts as engulfing
- required relationship to the prior candle
- meaning of “timely” and its maximum bar distance from the touch/Fibonacci interaction

### Entry, stop, target, and news

- exact entry order and timing after confirmation
- stop construction (not inferable from the screenshot)
- target construction (not inferable from the screenshot)
- calendar/source for fundamentals and the exclusion window

## Minimum additional source evidence needed

To build a deterministic source-faithful detector, obtain at least:

1. A primary Kojo rule description or annotated chart defining the structure
   zone, clean reactions, distinct visits, and departure.
2. A worked example with timestamps for reaction #1, departure #1, reaction #2,
   departure #2, and the exact third touch.
3. Explicit Fibonacci anchor selection and confirmation timing.
4. An exact bullish-engulfing definition and the allowed confirmation window.
5. Explicit entry timing/order semantics.
6. A historical-news source and the intended exclusion window.
7. Separate authoritative stop and target rules if trade reconstruction is
   required.

## Research boundary

```text
DISCOVERY_ACCESSED=false
VALIDATION_ACCESSED=false
PROFITABILITY_ACCESSED=false
PARAMETER_OPTIMIZATION=false
PRODUCTION_STRATEGY_CHANGED=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0
```

`CAN_BUILD_SOURCE_FAITHFUL_DETECTOR_NOW=false`

Reason: the source fixture is recovered, but the original 108-cell
implementation is lost and the source material does not define the remaining
mechanical semantics listed above. Implementing them now would turn
unsupported research hypotheses into invented strategy rules.
