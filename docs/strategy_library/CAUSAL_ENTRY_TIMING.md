# Causal entry timing v1

A completed setup and a completed entry candle are different events. A confirmed
next-open strategy resolves its pending proposal as soon as the provider opening
price is available, without using the entry candle's high, low or close. Strategies
whose decisions require a completed candle still wait for that confirmation.
Limit/stop trigger strategies retain their own trigger policy; they are not
silently converted into next-open market strategies.

## Shared contract and enforcement

`core.strategies.entry_timing.CandleOpenObservation` carries instrument, timeframe,
exact candle open time/price, provider availability, consumer observation, and
source identity. Validation rejects missing identity, nonfinite values, a wrong
candle/timeframe, invalid clock order and observations outside the 60-second entry
window. Price is the provider candle opening reference, not a promised executable
broker fill. No quote midpoint is substituted for it.

The collector retains opening evidence from the same snapshot it already fetches.
Only open time/price and first received availability are exported in this channel.
Repeated polls preserve first availability. An opening-price revision invalidates
that candle's observation until the next candle. This adds no bridge calls and does
not put incomplete candles into the completed-bar cache.

The composed runtime shares entry geometry/gap/R checks between replay and
`observe_open`. The five PA next-open evaluators use an external adapter around
copies of their frozen entry checks. The adapter never appends its internal
open-only carrier to setup history. Accepted/cancelled proposals are consumed;
checkpoints prevent another entry on replay or restart. All new entries remain
research-only. Existing canonical theoretical and broker outcomes remain separate.

At canonical publication, completed-bar PA, library reconstructions, completed-
candle entry mechanisms and Kojo BAR_CLOSE evidence cannot become prospective.
Any reference entry emitted more than 60 seconds later is retrospective regardless
of adapter-specific age settings. The reference timestamp is never rewritten to
make a signal look fresh. Explicit contemporaneous contracts require complete
availability evidence and matching immutable reference time. Old canonical rows
and their hashes are not updated.

The execution-intent factory reads canonical research/eligibility/classification
metadata and blocks research evidence before inserting an intent, for arbitrary
future strategy IDs too. The two frozen PA IDs also join the existing explicit
research deny set, protecting historical rows independently of their metadata.

## Coverage

| Strategy path | Timing behavior |
| --- | --- |
| 44 library compositions | Shared immediate open observation interface and replay parity; forward observer supports explicit new mode |
| S/R Retest, Engulfing Continuation | Frozen checks reused by opt-in PA runtime mode |
| Engulfing Reversal, Star Reversal, Pin-Bar Reversal | Same external immediate entry interface; no deployed observer registration added |
| Kojo V3 | Existing live quote-entry path retained; completed-bar fallback classified as research evidence |
| Context Retrace / Liquidity Displacement | Existing retracement/trigger rules retained; shared delayed/reconstruction publication safety applies |
| Future compositions | Use shared entry interface; do not implement another candle-close delay or infer opening prices from quotes |

D1 definitions still require a D1 provider/cache capability before deployment.
No claim of profitability, broker-realistic fills, or execution eligibility follows
from an immediate paper observation.

## Rollout boundary — not activated by this change

Do not repin the frozen library canary image or resume its checkpoint with changed
code. The library run binds config and artifact hashes. Create a separate run ID,
activation time, dataset/artifact binding and manifest with
`entry_mode: CONTEMPORANEOUS_CANDLE_OPEN`. Bootstrap remains warm-up only. Missing,
late, revised or unavailable opening evidence never creates a fresh entry; a later
completed-candle reconstruction remains separately labelled retrospective.

For PA, create a new managed observation instance and frozen manifest containing
`entry_mode: CONTEMPORANEOUS_CANDLE_OPEN`; set `PRICE_ACTION_ENTRY_MODE` to that
value. The runtime rejects both original frozen instance IDs in this mode and
rejects checkpoint mode changes. Existing mode defaults remain reconstructed.
Register the new instance through the existing managed catalog/orchestrator path;
do not overwrite old instance identity or historical evidence. The adapter carries
the explicit timing contract into canonical metadata. Broker execution stays off.

Deploy collector support first, then verify cached opening evidence and provider
availability. Build/pin a reviewed immutable runtime image for the new observation
run, verify source/parameter/primitive provenance, and check the first proposal's
confirmation → open availability → observation → publication timestamps. Missing
quotes do not authorize opening-price invention. No workload, manifest, registry,
DB migration or broker state has been changed by this source PR.
