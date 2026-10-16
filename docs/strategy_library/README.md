# Standardized Strategy Library — research implementation V1

This change implements **44 new causal compositions**, and catalogs **six existing
strategy identities** without replacing their logic. Together they cover ten
families with five definitions each. Context and Kojo remain additional preserved
strategies; they are not relabeled or incorporated into family performance.

This is an offline, executable research library using the existing canonical
`Evaluation`/`DecisionTrace` contracts and shared `research_outcomes` resolver.
It does not register, publish, deploy, route, or execute strategies. There are no
broker calls, database writes, production manifests, or account bindings.

All new definitions are `RESEARCH`, execution-ineligible, experimental V1s.
Their ICT/SMC names refer to the specific mechanical hypotheses below, not an
assertion that all discretionary interpretations of those methodologies agree.
There is no family-level performance claim. The additional Liquidity,
Session-Based and Swing/Position definitions are explicit initial research
hypotheses filling the previously named but unspecified families.

## Source of truth and identity

- `strategy_backtest/library/catalog.py`: immutable definition and complete
  experimental parameters; every named definition has a distinct ID.
- `primitives.py`: pure shared indicator, structure, pattern, zone, and session
  ingredients. Existing Price Action primitives are not replaced: these are new
  `library-primitives.v1` semantics, pinned separately to preserve legacy behavior.
- `compositions.py`: explicit mechanism branches, reused by definitions with
  distinct sessions/scopes/parameters. No per-strategy indicator copies.
- `runtime.py`: small research adapter to canonical evaluations and
  `ResearchOutcomeEntry`; not an alternative production orchestration service.
- `catalog.json`: generated full resolved manifests with source content hashes.
- `synthetic_report.json`: reproducible illustrative cases and episode evidence.

Version pins include the whole primitive semantic version, composition mode,
engine version, resolved parameter hash, exact source artifact digest and dataset
SHA-256. This coarse dependency pin is deliberately conservative: a change to any
shared implementation changes the artifact identity. Parameter-set IDs contain
the strategy/version and resolved-configuration hash; no legacy-null fallback.
Future behavior changes require separately identified versions/evidence.

## Universal temporal and trade contract

Input is one canonical instrument, strictly ordered completed UTC-open candles.
M15/H1/H4/D1 durations and alignment are validated. Duplicate/revised snapshots,
future candles, invalid OHLC, nonpositive/unknown tick sizes, changing tick sizes,
and naive evaluation timestamps are rejected. Snapshot rejection is atomic.
Source `data_available_at`, when present, must not exceed the evaluation cutoff.
When unavailable, close-time availability is an explicit assumption, not proof of
what a historical/live feed knew. Live integration must supply availability and
processing times separately and must not present reconstructed opens as fills.

Execution/context timeframes and experiment settings are explicit in each
manifest. Context-dependent hypotheses require the most recently completed HTF
boundary and contiguous retained context. Detection excludes future context.
Strict confirmed pivots require two bars on each side; a pivot becomes available
at the second right-hand close. Trend requires two higher highs AND higher lows,
or two lower highs AND lower lows. Neutral/insufficient structure is not trend.
Execution setup sequences require contiguous bars. Missing calendar/session
constituents fail closed: there is no inferred holiday/market-closure calendar.
UTC-day/week ranges therefore need complete data; ordinary FX weekend gaps can
block a complete-week hypothesis until an explicit calendar policy is introduced.

ATR is the arithmetic mean of the last 14 true ranges from **prior** bars,
excluding the signal candle. EMA uses a first-n-bar SMA seed and alpha 2/(n+1),
with a stable full-prefix seed; RSI is Cutler RSI, not Wilder RSI. Bollinger bands
use prior 20 closes and population standard deviation. VWAP uses positive finite
**tick volume** and typical price, not consolidated traded volume. Unavailable
volume rejects VWAP hypotheses instead of assuming unit volume.

New proposals use signal-close geometry. Stops use the last five execution-bar
extreme plus `max(one tick, 0.1 prior ATR)` unless the hypothesis defines a zone,
range, sweep, or impulse stop. Fixed-R targets freeze at 2R relative to that
proposed close; mean/VWAP/opposite-range targets freeze their known reference.
Minimum planned R is 1.0. The configuration is experimental, not optimized.

Entry eligibility is reconstructed at the exact next execution candle's open.
It uses the open and prior frozen geometry only; subsequent high/low/close do not
influence acceptance. Missing next candle, session expiry, absolute open-to-close
gap above 0.25 prior ATR, invalid geometry, or actual-entry R below minimum cancel.
This gap is price movement from signal close, not distance to a structural zone.
Every accepted entry is clearly `COMPLETED_CANDLE_RECONSTRUCTION`, never a broker
fill or executable signal. Observed/availability times remain unknown if absent.

Episodes are keyed by strategy/version, parameters, instrument, direction and
setup anchor. A zone/break anchor is consumed once; session hypotheses allow one
entry per direction per session. Rolling breakout/cross/indicator events use their
confirmation timestamp as anchor. Positions are independent theoretical
observations, so overlapping episodes may exist; this is not a portfolio model.
Checkpoints bind data/artifact/configuration/dependencies and copy nested state.
Evaluation traces are labeled L2: stage summaries, not a claim of fully expanded
primitive-by-primitive L3 traces. Strategy-specific reasons are registered under
`LIBRARY_*` without changing existing reason semantics.

## Sequence/zone contract

- Liquidity sweep: wick strictly beyond a prior level/channel and close strictly
  reclaimed inside it. Equal-level sweeps use the outer extreme of the latest two
  confirmed same-kind pivots within 0.15 ATR.
- Displacement: directional body >=1.2 prior ATR and >=65% of range; parameterized
  exceptions are explicit in the manifest. Local break uses prior five-bar range.
- Confirmation: strict opposing body engulfing, or directional rejection with
  wick >=2 bodies, opposite wick <=1 body, close in directional outer 35%.
- FVG: strict first/third wick gap of three completed candles; known at third
  close. The displacement must be the middle candle. Retest is later, with zone
  intersection, directional close outside its far edge, and confirmation.
- Order block: last opposing candle within five bars preceding displacement that
  closes beyond the prior 20-bar channel. Full wick range, known at displacement
  close. Retest is later, with the same zone confirmation contract.
- Zones expire after eight execution bars from their event search window; a close
  through the far invalidation edge before entry rejects the zone. An earlier
  touch alone does not invalidate it; its episode cannot enter twice.
- BOS retest requires HTF directional bias; CHOCH requires an established opposing
  execution structural trend. The break must cross a previously confirmed swing;
  a subsequent candle retests the frozen break level. No ambiguous MSS alias.
- Trendline break uses latest two confirmed directional pivots with matching slope;
  the projected line is evaluated at the break, then its break price is frozen as
  the retest reference. It is not a hindsight refitted line.
- OTE uses the latest confirmed opposite-kind consecutive HTF pivot impulse in
  trend direction, and its 62–79% retracement zone, available after both anchors.
- ICT 2022/Silver Bullet: HTF bias → prior-channel sweep/reclaim within five bars
  before displacement → local five-bar close break → displacement-middle FVG →
  later rejection retest within eight-bar search. Silver Bullet additionally
  requires confirmation inside its explicit NY window; these are research models.

## Sessions

Sessions use IANA timezones and local-date identities. Cross-midnight range
support anchors to the start date. Catalog endpoints avoid DST transition hours;
ambiguous/nonexistent local endpoints are rejected. DST shifts UTC boundaries.
The OS timezone database is a runtime dependency; archive its version/container
with a historical experiment. Calendar-free session proxies do not claim an
exchange's official trading session for OTC FX/crypto.

Range constituents must exactly cover the configured opening/range interval.
The range freezes before breakout evaluation. Confirmation closes must be strictly
after trade start and before trade end; entry open must precede trade end. Wick-only
breaks and closes equal to a boundary do not qualify. Opening Range Break uses
09:30–10:00 NY range; NY Open Breakout uses 08:00–09:30 NY pre-open range: distinct
hypotheses, sharing the same mechanism implementation.

## Replay

```bash
PYTHONPATH=. python -m research.strategy_library catalog --output docs/strategy_library/catalog.json
PYTHONPATH=. python -m research.strategy_library synthetic --output docs/strategy_library/synthetic_report.json
PYTHONPATH=. python -m research.strategy_library replay --key donchian_breakout --dataset /path/audited.json --output /path/report.json
```

Dataset JSON requires `source`, `timestamp_semantics: UTC_OPEN_SECONDS`, positive
`tick_size`, an `evidence_class`, and `bars`/`context` arrays of MarketEvent records.
Each record supplies canonical instrument, timeframe, open/close Unix timestamps,
OHLC, completed flag, source and optional provenance. Tick volume belongs in
`provenance.tick_volume`. Optional `expected_gaps` must be independently audited
for the outcome resolver; they do not waive detector completeness requirements.

The dataset file is hashed before replay. Every evaluated prefix uses the same
full-prefix indicator seed. The CLI feeds accepted research entries into the
existing shared resolver, preserving ambiguity, missing paths and end censoring.
No invented maximum hold, Trade Manager intervention, or broker-realistic cost
model is introduced. Results are gross reference-price observations. The CLI
reports episode counts and resolved statuses, not automatic graduation metrics.

The synthetic report has LONG/SHORT setup→entry cases for all 44 new definitions.
They are deliberately crafted fixtures, never market performance or holdout
validation. Existing six definitions keep their separate original replay tooling.
Historical validation for new definitions is NOT RUN: the checkout has no audited
common H1/M15/H4/D1 dataset suitable for the entire library. Existing manifested
historical exports are unavailable here; this does not erase their prior evidence.

## Next operational boundary

Before any new observer activation, review/freeze each experimental definition,
audit instrument metadata and data coverage, run prospective/replay comparisons,
and integrate the same evaluator through managed registration and isolated
observer infrastructure. This patch does not activate any of those steps.
Historical or forward evidence belongs to version/parameters/instrument/session/
data/outcome-policy provenance, never generically to a family.
