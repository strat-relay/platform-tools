# Donchian Breakout V2 — frozen research hypothesis

Status: RESEARCH. Strategy ID BREAKOUT_DONCHIAN_BREAKOUT, version V2.
Separate parameter/dependency/artifact/dataset identity; V1 is unchanged.
The recipe is opt-in through DonchianV2Runtime and the explicit forward selector
`donchian_breakout@V2`. Its dedicated registration and activation are generated
separately; importing code does not register, deploy or activate it. The unqualified
`donchian_breakout` selector retains V1 semantics.

## Hypothesis and scope

A prior-channel breakout aligned with confirmed higher-timeframe structure,
followed by a first later retest that holds, may provide better continuation
entries than the unfiltered V1 close-break. Additional entries require fresh
pullbacks and favorable progress. This is an unvalidated hypothesis.

The thresholds below are engineering research assumptions, not fitted optima.
The previously inspected Donchian observations are development evidence and
cannot be claimed as untouched validation. No win probability is assigned.

## Completed-bar ingredients

- M15 execution, H1 context. At least 40 contiguous completed M15 bars and
  10 completed H1 bars; latest H1 close must equal floor(M15 cutoff/H1).
  The latest up-to-80 H1 bars must be contiguous. Missing context/gaps fail closed
  and clear the episode. No weekend/holiday schedule is guessed.
- Reuse library-primitives.v1 strict confirmed swings: two left/two right bars,
  with the right confirmation completed before use. Latest two confirmed highs
  and lows must both rise (LONG) or fall (SHORT). No mixed/unknown bias entry.
- Prior 20-M15-bar wick channel excludes the breakout candle.
- ATR14 is the arithmetic mean of true ranges on prior bars, excluding the
  current candle. The breakout's ATR is frozen for retest tolerance and minimum
  episode layer progress. Stops and next-open gap checks use confirmation's
  prior ATR. Threshold values are part of the explicit V2 parameter manifest.

## Initial setup

1. In IDLE, require a completed close beyond the trend-direction channel boundary
   by at least 0.20 prior ATR. Directional candle body must be at least 60% of
   high-low range; close must lie in the top/bottom 25% in trade direction.
2. Freeze boundary, breakout time, direction and ATR. Do not propose entry on
   the breakout candle. WAIT_RETEST begins.
3. Within the next eight M15 closes, the first later candle intersecting the
   boundary band +/-0.15 frozen ATR must hold. Its close must be strictly outside
   the band in the breakout direction, its body must be directional and at least
   25% of its range, and close location must be at least 65% in that direction.
4. A first touch that fails is not replaced with a later prettier touch. Expired
   or failed setups enter WAIT_RESET. A close back to/inside the frozen boundary
   rearms IDLE on a later bar; no new setup is created on the reset bar itself.
5. A close beyond the wrong side of the boundary by more than tolerance, or a
   confirmed bias change, invalidates the episode immediately, without a proposal
   on that bar. Invalidation also clears ACTIVE/PULLBACK episodes.
6. Initial stop is beyond the adverse extreme from breakout through retest,
   including both bars, buffered by max(one explicit provider tick, 0.10 prior
   confirmation ATR). Planned target is 2R from confirmation close.

## Entry and evidence

Use the shared next-open entry gates. Exact next M15 opening timestamp, valid
stop/target geometry, at least 1R actual-entry opportunity and opening gap no
larger than 0.25 prior confirmation ATR are required. A canceled entry moves its
remaining episode to WAIT_RESET. No arbitrary current quote substitutes for open.

Completed-candle replay reconstructs the opening price using only that candle's
open for entry eligibility; high/low/close never select or price that entry.
It remains COMPLETED_CANDLE_RECONSTRUCTION and research-only. A contemporaneous
path accepts only the existing CandleOpenObservation contract with provider
identity, confirmed setup, availability ordering and <=60s observation window.
The same frozen entry/geometry gates apply. This PR wires neither path to a broker
nor to a canonical publisher. Publisher guards remain authoritative if a future
integration is authorized. No timestamps are shifted to make old prices actionable.

## Layering

- An accepted entry advances the episode to ACTIVE. Consecutive new highs/lows
  alone cannot generate additions. Entry candle is not eligible as a fresh
  pullback; its completed favorable extreme is recorded consistently in both
  reconstructed and observed-open paths.
- A later adverse close versus the preceding close starts PULLBACK. Freeze the
  favorable extreme observed before this pullback as the continuation boundary.
- A separate later candle, within eight M15 closes of pullback start, must close
  beyond that frozen extreme by more than one tick, satisfy the initial candle
  body/location conditions, and progress at least 0.50 episode ATR beyond the
  previous accepted entry price. The actual next open must retain that progress.
- Stop is beyond the full pullback-to-confirmation adverse extreme with the same
  ATR/tick buffer; target is a newly frozen planned 2R. Initial risk is immutable.
- No layer occurs while an entry is pending. A fresh pullback/continuation is
  required after each accepted addition. Each candidate has its own immutable
  episode ID, with the original breakout episode/time in evidence.
- These are research episode additions. The runtime has no broker positions,
  account exposure, sizing, prior-trade outcome or P/L authority. It does not
  assert that an earlier trade remains open, increase lot sizes or implement
  martingale. Future execution must impose separate portfolio risk policies.

## State, identity and reuse

The reusable pure breakout-retest-episode.v1 transition consumes only validated
causal bars and returns new state; it performs no I/O. DonchianV2Runtime composes
that state machine with the existing ATR/swings/trend primitives and shared
ComposedResearchRuntime entry lifecycle. It does not duplicate fill or outcome
logic. Per-instrument state and checkpoints bind V2 parameters, source artifact,
dataset and dependency versions. Invalid input/revisions and tick changes are
rejected atomically. Repeated cutoffs are idempotent. Research entry acceptances
are not executable signals; canonical Evaluation decisions remain research rejects.

## Replay and validation

```bash
PYTHONPATH=. python -m research.replay_donchian_v2
PYTHONPATH=. python -m research.replay_donchian_v2 --audit-report /path/to/report.json
```

The default deterministic synthetic replay compares V1/V2 on shared inputs and
covers an initial entry and a later layer. It is a lifecycle check, not performance
validation. The optional audit replay reads frozen tick evidence and reference M15
bars. H1 is aggregated only from four exact completed M15 constituents, explicitly
recorded as derived context rather than native H1. It does not infer missing bars.
All supplied audit data is DEVELOPMENT_ONLY_NOT_VALIDATION. The bounded history
and restarted research run do not reconcile the original forward V1 run.

Before any graduation: freeze an audited native H1/M15 dataset with provider tick
metadata and causal availability assumptions; predeclare chronological development,
validation and holdout boundaries plus clustered episode analysis; compare V1,
trend-only, breakout-quality-only, retest-only and full-recipe research arms through
shared primitives. Those empirical ablations are pending, not claimed by unit tests.
Do not use the 54 inspected entries as holdout. No dataset or statistical validation
is currently claimed. Report costs, gaps, ambiguous/open outcomes and overlapping
exposure separately; use the shared frozen theoretical resolver, not broker P/L.
