# KOJO_STRUCTURE_FIB Source-Fidelity V2

## Scope

This is a new causal, descriptive source-fidelity investigation from base
commit `2524387`. It is not a reconstruction of the lost 108-cell study, not
a detector, and not a profitability study.

## Fixture verification

```text
FIXTURE_SHA256=fbb5ec907c8f6bd979e49012eea0510ba06dcc50fa00916f8776500d91de8af9
FIXTURE_SHA256_MATCH=true
BAR_COUNT=183
PROVIDER=OANDA
SYMBOL=XAUUSD
TIMEFRAME=H1
```

## Source structure

`SOURCE_EXPLICIT`: Kojo describes a third touch after two clean reactions.
`SOURCE_FIXTURE_OBSERVATION`: the visually indicated region is around 4200.
`RESEARCH_HYPOTHESIS`: a descriptive 4195–4205 observation band is used only
to enumerate raw candle intersections. It is not a threshold or detector rule.

The fixture contains `47` raw candle intersections with that band
between Dec 8 00:00 and Dec 11 12:00 UTC. These are not asserted to be
distinct visits. Grouping consecutive intersections and defining departure
requires source semantics that are not available.

`TOTAL_PRE_CONFIRMATION_INTERACTIONS=47` (raw candle intersections)
`LITERAL_THIRD_TOUCH_POSSIBLE=UNRESOLVED`

## Structure lifecycle and clean reactions

Several birth candidates are plausible from the visible price history, but no
one birth time is source-supported. A literal ordinal interpretation is
possible only after defining distinct visits and meaningful departure. Clean
reaction status is not mechanically determinable from the source material.

`NATURAL_EXACT_THIRD_TOUCH_INTERPRETATION=UNRESOLVED`
`CLEAN_REACTION_MECHANICALLY_DETERMINABLE_FROM_SOURCE=false`
`EARLIER_INTERACTIONS_EXIST=true`

## Fibonacci forensics

Three causally available bullish impulse candidates are recorded in
`fib_forensics.json`. More than one can produce a pocket near the observed
Dec 11 entry region, so the source does not uniquely identify the anchors.

`PLAUSIBLE_FIB_ANCHORS=3`
`MATCHING_FIB_ANCHORS=2`
`FIB_ANCHOR_UNIQUE_FROM_SOURCE=false`

## Engulfing forensics

The Dec 11 06:00 and 12:00 candidates are evaluated with strict body,
0.005-tolerant body, and full-range conventions in
`engulfing_forensics.json`. These are standard diagnostic definitions only;
the source does not select one uniquely.

`SOURCE_CONFIRMATION_CANDLE_MOST_PLAUSIBLE=UNRESOLVED`
`ENGULFING_DEFINITION_UNIQUE_FROM_SOURCE=false`

## Entry, stop, and target

The screenshot values are retained as source-fixture observations only.
`ENTRY_RULE_DETERMINABLE=false`, `STOP_RULE_DETERMINABLE=false`, and
`TARGET_RULE_DETERMINABLE=false`. No rule is inferred from one screenshot.

## Dec 10 negative control

Dec 10 13:00 is recorded independently in `dec10_negative_control.json`.
The current source evidence does not mechanically establish whether it is a
third touch, has Fibonacci confluence, or has source-defined engulfing. The
earliest defensible conclusion is that no source-supported distinction can be
asserted without inventing a filter.

`DEC10_STRUCTURE_THIRD_TOUCH=UNRESOLVED`
`DEC10_FIB_CONFLUENCE=UNRESOLVED`
`DEC10_BULLISH_ENGULFING=UNRESOLVED`
`DEC10_WOULD_LOOK_VALID_UNDER_CURRENT_KNOWN_SOURCE=UNRESOLVED`
`SOURCE_SUPPORTS_USING_THIS_DIVERGENCE=false`

## Source-fidelity table

| Semantic | Source status | Mechanically determinable | Evidence / ambiguity |
|---|---|---:|---|
| structure | SOURCE_EXPLICIT + UNRESOLVED | false | structure is named, level/zone mechanics absent |
| structure birth | UNRESOLVED | false | no creation rule |
| clean reaction | SOURCE_EXPLICIT + UNRESOLVED | false | “clean” is qualitative |
| distinct visit | UNRESOLVED | false | consecutive-touch grouping absent |
| third touch | SOURCE_EXPLICIT | partial | ordinal phrase exists; visit/departure semantics absent |
| Fib anchor | SOURCE_EXPLICIT + UNRESOLVED | false | pocket exists; anchor algorithm absent |
| Fib pocket | SOURCE_EXPLICIT | partial | 61.8–78.6% stated |
| engulfing | SOURCE_EXPLICIT + UNRESOLVED | false | bullish engulfing stated; exact body/wick semantics absent |
| timely | SOURCE_EXPLICIT + UNRESOLVED | false | no timing window |
| direction | SOURCE_FIXTURE_OBSERVATION | true | source setup described as bullish/long |
| entry | UNRESOLVED | false | screenshot approximation only |
| stop | UNRESOLVED | false | screenshot approximation only |
| target | UNRESOLVED | false | screenshot approximation only |
| news exclusion | SOURCE_EXPLICIT + UNRESOLVED | false | calendar/source/window absent |

## Detector readiness

```text
SOURCE_SETUP_CAUSALLY_RECONSTRUCTIBLE=PARTIAL
SOURCE_SETUP_UNIQUELY_RECONSTRUCTIBLE=false
DEC10_DISTINGUISHABLE_WITH_SOURCE_SUPPORTED_RULES=false
CAN_BUILD_SOURCE_FAITHFUL_DETECTOR_NOW=false
READY_FOR_BACKTEST=false
```

The minimum missing information is: structure-zone mechanics, lifecycle/birth,
clean reaction qualification, distinct visit/departure semantics, exact Fib
anchors, engulfing definition, timing window, entry semantics, news source and
window, and authoritative stop/target rules.

```text
DISCOVERY_ACCESSED=false
VALIDATION_ACCESSED=false
PROFITABILITY_ACCESSED=false
PARAMETER_OPTIMIZATION=false
STRATEGY_IMPLEMENTED=false
PRODUCTION_STRATEGY_CHANGED=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0
```
