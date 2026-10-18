# Frozen ICT / SMC research expansion

These are explicit mechanical hypotheses, not authoritative definitions of discretionary ICT or SMC. Existing Judas Swing V1 and Order Block Retracement V1 retain their catalog manifests, evaluator modes and version identities. New versions are opt-in via `strategy_backtest.library.expansion.BY_REF`, not added to the deployed canary selection or original fifty-strategy catalog.

| Family | Strategy reference | Definition |
| --- | --- | --- |
| ICT | ICT_JUDAS_SWING@V2 | London range sweep, later reclaim, later displacement/BOS |
| ICT | ICT_POWER_OF_THREE@V1 | Accumulation, manipulation, distribution |
| SMC | SMC_ORDER_BLOCK_RETRACEMENT@V2 | First-touch retest of a displacement-confirmed order block |

## Common causal contract

Completed M15 candles, chronological UTC candle-open seconds, positive explicit provider tick size. ATR is prior-candle SMA true range14; signal candle excluded. A displacement requires directional body >=1.2 prior ATR and body/range >=65%. BOS here means a strict close beyond the prior five-bar channel for session sequences, or prior twenty-bar channel for block formation; it is not CHOCH or a discretionary MSS claim. Recent execution gaps fail closed. Exact session coverage is mandatory; no guessed holiday/calendar filling.

Library primitives remain `library-primitives.v1`. New sequence primitives are `sequence-primitives.v1`, explicitly bound to the dependency hash. Versions, parameters, artifact hash, dataset hash, episode identity and primitive dependencies flow through the shared runtime into canonical research evaluations and theoretical entries. Version-aware IDs/checkpoints prevent V1/V2 collisions. Existing V1 runtime strings and dependency hashes remain unchanged for the same input artifact. Changing source artifact identity intentionally requires a separately identified run.

Confirmation uses the completed candle close. Intended entry is the immediately following M15 open. Replay can only reconstruct this open from the next completed candle; it is never a live fill or an actionable retrospective signal. The shared contemporaneous-opening observation API remains available for future authorized runtime integration. Entry cancellation and immutable initial risk use the existing SDK. Maximum absolute next-open movement is 0.25 confirmation ATR; actual entry must preserve valid geometry and >=1R. No invented confidence, historical validation or execution eligibility is attached.

## Judas Swing V2

Freeze the complete 00:00–05:00 Europe/London M15 high/low. Search 07:00–10:00 London, using IANA DST. The first strict wick breach chooses direction: lower edge sweep implies LONG, upper edge SHORT. A candle breaching both edges rejects the session as ambiguous. Reclaim must occur on a later candle with a strict close inside the range, followed by a still later displacement/BOS close within eight bars after sweep. An opposite-edge breach before reclaim invalidates the sequence. First qualifying confirmation wins; later confirmations cannot create another episode.

Stop is the sweep-to-confirmation extreme plus max(one tick, 0.1 prior ATR) buffer. Target is the opposite frozen range edge; reject <1R or invalid geometry. Confirmation and next-open entry must be strictly before 10:00. Session date/direction identifies the episode.

## Power of Three V1

Freeze complete 00:00–06:00 America/New_York accumulation. Width must be <=3 ATR measured from completed inputs at range end. Manipulation is the first range breach from 07:00, and must begin before 10:00 New York. Require a later reclaim and later displacement/BOS as above, additionally closing beyond the opposite accumulation edge to confirm distribution. Confirmation/entry deadline 12:00 New York; the same eight-bar sequence expiry applies. Stop uses the sequence extreme/buffer; target is 2R frozen from confirmation. This is one intraday interpretation, not a daily-candle forecast or guaranteed range expansion.

## Order Block Retracement V2

Require current causal completed H1 context with two strictly confirmed highs/lows producing HH/HL or LH/LL (two bars on each side to confirm pivots). In that bias, a displacement strictly closes beyond the prior twenty-bar channel. The last opposing candle within five preceding bars defines the full wick-range block, known only at displacement close. No block without both displacement and BOS; dojis are not opposing candles.

Only a later first zone intersection may qualify, within eight M15 candles of formation. A strict close beyond the distal edge invalidates the block, including a gap without intersection. First touch consumes the block even if rejection fails. For LONG the touch candle must close above the block high, with existing strict engulfing or rejection-wick confirmation (mirrored SHORT). Stop spans block and rejection extremes plus buffer; fixed 2R target. Formation close identifies the episode. No same-candle formation/entry, implicit order-flow claim, later recycled mitigation or broker execution.

## Research and rollout

Run `python -m research.ict_smc_expansion synthetic --output /tmp/ict-smc-replay.json` for deterministic long/short lifecycle replay. The `catalog` command exports manifests. `replay --ref ... --dataset ...` uses the existing audited JSON dataset contract and shared theoretical resolver; explicit source, evidence class, UTC_OPEN_SECONDS, tick_size, bars and context are required. Historical validation has not been run. Synthetic pass counts are software checks, not market evidence.

No registration SQL, migrations, workloads, deployment, forward activation, execution policy, broker writes or historical evidence changes are included. Family labels are an immutable display taxonomy; performance attribution remains at concrete version/parameter/run provenance. Context/Kojo are classified under Price Action for display; unmapped historical IDs remain Unclassified rather than inferred from arbitrary strings.
