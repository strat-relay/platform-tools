# Signal Detail Evidence Audit and Design Direction

Status: audit/design only. No strategy, signal-generation, execution, broker,
or runtime-service behavior is changed by this document.

## Scope

Signal Detail must eventually render evidence captured during the signal
lifecycle. It must not reconstruct strategy reasoning from current market
data, use later candles as decision evidence, or infer broker fills from
strategy/economic entries.

## Current pipeline

`context_structure_retrace_forward.py` produces setup, opportunity, and
economic-position state. The Phase 6 compact state retains a causal setup
snapshot, event bar, context/zone data, entry mechanisms, geometry, IDs,
provenance, lifecycle status, and MFE/MAE. Its append-only event stream records
setup detection, fill, invalidation, target, and stop transitions.

`orchestration.adapters.context_structure_retrace` discovers filled economic
positions and canonicalizes them into immutable `StrategySignal` records.
`StrategySignal` retains strategy/config identity, setup/opportunity/economic
IDs, symbol/timeframes, event and signal timestamps, entry/stop/target,
entry mechanisms, selected strategy metadata, and source provenance. It does
not retain the full setup snapshot, event candle, context zones, indicator
values, or the decision-time candle window.

`signal_orchestrator.py` persists canonical signals, route decisions,
distribution entries, sizing decisions, account snapshots, and generic
orchestration events. It does not independently recompute technical analysis.

Phase 7 and Trade Manager publish causal market observations and lifecycle
updates keyed primarily by `economic_position_id` and `setup_id`. These include
market candles/history, price semantics, MFE/MAE, and threshold/checkpoint
observations, but are not a canonical terminal-outcome evidence record.

## Audit findings

### Decision evidence already reaching the orchestrator

Available in `signals.jsonl`:

- strategy ID/version/instance and source event ID;
- setup, entry opportunity, and economic position IDs when supplied;
- market event ID, symbol/canonical/broker symbol, timeframes, direction;
- signal/decision/publication timestamps;
- entry type, entry price, stop, target, risk/target distances and target R;
- entry mechanism tuple;
- limited `strategy_metadata` (for Context V1: pattern, reentry type, V1 status);
- source/config/decision fingerprint and continuity/health provenance when the
  producer supplied it.

The strategy's actual `context_snapshot`, event candle, context components,
zones, indicator values, qualification evidence, and explicit invalidation
criteria are not carried into `StrategySignal`.

### Candle and context evidence already present

Phase 6 setup state contains a compact causal snapshot with completed-candle
timeframe context, ATR/EMA/SR-derived fields and zones, plus the setup event
candle and provenance asserting completed-candle-only evaluation. Economic
positions retain the fill candle and selected geometry. Phase 7 observations
retain causal M5 history and market observations, with `future_data_used` set
false for those observation records.

This evidence remains in strategy-specific files and is not linked into the
canonical signal record as an immutable decision-evidence bundle. The compact
projection is also not a general cross-strategy contract.

### What is lost at signal publication

- full setup evidence and structure/context snapshot;
- levels/zones as seen at decision time;
- indicators actually used and their values;
- trigger/reason as a structured causal record;
- intended entry/stop/target rationale and invalidation criteria;
- exact decision-time candle/window envelope and its source timestamps;
- strategy/config/freeze identity as a complete immutable bundle;
- an explicit distinction between decision-time evidence and later context;
- for liquidity-displacement signals, `economic_position_id` is currently
  absent and `market_event_id` is absent in the adapter output.

### How terminal outcomes currently reach the signal record

For Context V1, the strategy mutates the economic-position record to
`TARGET_HIT` or `STOPPED`, writes a corresponding Phase 6 event, and the
adapter later reads that state. The canonical signal's `strategy_metadata`
may therefore contain `v1_status`, but the orchestrator does not receive a
terminal lifecycle event as a first-class signal record transition.

Pre-entry invalidation is recorded in the Phase 6 event stream and setup
state, but no canonical `StrategySignal` is emitted for it. `TIME_EXIT` and
`BREAKEVEN` are not part of the active Context V1 terminal path. Other
research runners use different labels such as `STOPPED`, `TARGET_HIT`,
`TIME_EXIT`, `INVALIDATED`, or `EXPIRED`.

### Whether terminal outcomes contain market snapshots

No authoritative outcome snapshot is currently attached to the terminal
outcome event or canonical signal. Phase 6 terminal events contain event
time, IDs, status/reason, realized R, and aggregate MFE/MAE; they do not
contain the terminal candle, bid/ask/quote, spread, or a bounded market
window. Phase 7 can observe market data around the lifecycle and mark
post-exit shadow measurements internally, but its shared lifecycle update
does not establish an outcome-time snapshot tied to the authoritative
terminal transition.

The event `event_time` is also the writer's wall-clock time, not a durable
market-event timestamp. The position state has `exit_timestamp`, but that
timestamp is not currently promoted into a terminal evidence envelope.

### Where lifecycle evidence is persisted

- strategy lifecycle: `context_structure_retrace_forward.jsonl`;
- current compact strategy state: `context_structure_retrace_forward_state_compact.json`;
- canonical signals and orchestration lifecycle/disposition: `runtime/orchestration/*.jsonl`;
- Phase 7 causal observations and shared position updates:
  `runtime/trade_manager/observation_stream/events.jsonl`;
- execution intents, market snapshots, decisions and skips:
  `runtime/execution/*.jsonl`.

These are separate streams. There is no single durable signal-lifecycle
evidence aggregate that joins them with immutable provenance and terminal
market state.

### Existing ID connectivity

Context V1 currently connects `setup_id -> entry_opportunity_id ->
economic_position_id -> signal_id` through the adapter's deterministic
`source_event_id`/stable ID derivation. `StrategySignal` also carries
`strategy_instance_id`, `strategy_id`, `strategy_version`, and `market_event_id`.
Phase 7 and Trade Manager reliably carry `setup_id` and
`economic_position_id`, but generally do not carry `signal_id` or
`entry_opportunity_id`. Execution intents carry signal, opportunity, economic
position, strategy instance, and sizing IDs, but failed/DRY_RUN intents are
not broker fills and must not be used as evidence of a fill.

Liquidity Displacement currently maps `setup_id` into both setup and
opportunity fields and leaves `economic_position_id`/`market_event_id` empty;
this is a known identity gap.

## Minimum additions for historically reproducible Signal Detail

1. Add a versioned, immutable `decision_evidence` envelope to the strategy
   output contract. It should include the strategy-owned setup evidence,
   structure, levels/zones, used indicators, trigger/reason, intended trade
   geometry, invalidation criteria, config/freeze identity, and a bounded
   decision-time candle/context window with per-series as-of timestamps.
2. Persist that envelope unchanged in the orchestrator's signal record and
   publish a content hash plus schema/version identifiers. The orchestrator
   may validate and store it, but must not calculate or rewrite technical
   analysis.
3. Add a durable signal lifecycle stream keyed by `signal_id`, with explicit
   transitions and causation IDs. Preserve the existing setup/opportunity/
   economic-position links and fill them for every strategy that emits a
   signal.
4. Add an authoritative `outcome_evidence` record for every terminal
   economic outcome. It must include terminal reason, market-event timestamp,
   quote/candle snapshot, source/provenance, realized state, and the exact
   lifecycle entity IDs. Capture it at the outcome boundary, not by a later
   frontend query.
5. Persist the active-signal market path as an append-only sequence of
   timestamped, causally bounded observations, with explicit entry and
   terminal boundaries and no post-outcome rows in the evidence interval.
6. Store optional post-outcome candles in a separate, visibly labeled
   `post_outcome_context` stream. It must never be merged into decision or
   active-path evidence.
7. Keep strategy/economic entry facts separate from broker execution facts.
   A broker fill may be linked only when an execution adapter receives and
   persists an authoritative broker fill; otherwise Signal Detail must label
   the entry as strategy/economic/paper-observed.

## Intended future Signal Detail model

The frontend should consume four server-produced sections:

1. `pre_signal_decision_snapshot`
2. `active_signal_market_path`
3. `outcome_snapshot`
4. optional `post_outcome_context` (explicitly labeled)

The frontend should render these sections and their provenance, not derive
strategy reasoning from present-day or later historical data.

## Export status

The approved read-only historical export was attempted for the 15-symbol,
2025-09-19 through 2026-09-19 UTC range. The research bridge at
`127.0.0.1:22350` was unavailable, so the exporter failed closed with zero
broker writes and readiness blocked on feed identity/symbol resolution. No
runtime service was restarted as part of this audit.
