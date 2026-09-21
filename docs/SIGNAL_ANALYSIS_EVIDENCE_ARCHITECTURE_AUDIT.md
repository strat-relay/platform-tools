# Signal Analysis Evidence Architecture — Audit and Design

Status: audit/design only. This document records the direction for upcoming
Signal Analysis work. It does not change strategy logic, signal generation,
execution, broker paths, or runtime services.

## Architectural decision

Signal Detail must render evidence captured during the signal lifecycle. The
frontend must not reconstruct strategy reasoning from current or later market
data.

The evidence boundary is:

```text
Strategy
  -> immutable decision evidence
Signal Orchestrator
  -> durable signal registration and lifecycle evidence
Market/economic outcome registrar
  -> terminal outcome and outcome-time market evidence
Signal Detail
  -> read-only rendering of persisted evidence
```

The orchestrator registers and transports strategy evidence; it does not
recalculate technical analysis. Broker fills remain a separate execution fact
and are never inferred from strategy or economic entries.

## Current pipeline audit

### Strategy -> orchestrator decision evidence

`context_structure_retrace_forward.py` currently produces setup and economic
position records containing, where available:

- `setup_id`, `market_event_id`, symbol, direction, pattern, setup timestamp;
- the setup event bar OHLCV/spread;
- a causal context snapshot and compact structure zones;
- qualification flags, entry level, theoretical entry, spread at detection;
- entry opportunity and economic position IDs;
- fill timestamp, entry mechanism, theoretical and executable paper entry;
- stop, target, geometry, target R, MFE/MAE, and lifecycle status;
- strategy, configuration, Phase 2, freeze, source-read, and gap-recovery
  provenance in the records that carry it.

`ContextStructureRetraceAdapter` maps this state into immutable
`StrategySignal` records. The signal currently reaches the orchestrator with:

- `signal_id`, strategy/version, `strategy_instance_id`, and `source_event_id`;
- `market_event_id`, `setup_id`, `entry_opportunity_id`, and
  `economic_position_id`;
- symbol/timeframe/direction and entry/stop/target geometry;
- entry mechanisms and limited `strategy_metadata` such as pattern and
  reentry type;
- `decision_time`, `signal_emitted_at`, source market timestamp/read health,
  source data age, configuration hash, strategy fingerprint, and freeze
  classification.

This is enough to identify and route a signal, but not enough to reproduce the
decision view.

### Candle and context evidence that already exists

The strategy state retains a compact subset of decision context:

- the setup event candle;
- completed-candle-only provenance;
- the structure timeframe and selected zone bounds/roles;
- M15 ATR;
- lifecycle and entry geometry.

The original full state and forward event stream contain more context than the
orchestrator signal, but they are local implementation artifacts rather than a
versioned signal evidence contract. The event stream mostly records lifecycle
facts and scalar outcome metrics, not immutable candle windows.

The Phase 7 observation stream separately persists periodic post-entry market
observations and M5 history. It explicitly marks `future_data_used: false`, but
it is an active-signal observation stream, not the original decision snapshot.

### What is lost at signal publication

The following information is currently absent from the durable canonical signal
record or only indirectly recoverable:

- the complete decision-time market snapshot/window across M5, M15, H1, and H4;
- the exact setup/trigger candle window used by the strategy;
- full structure zones, EMA values/order/slopes, HTF direction, and indicator
  values actually used;
- explicit trigger and reason evidence as a structured object;
- explicit invalidation criteria as a structured object;
- the complete intended-target set rather than one scalar target;
- exact source timestamps for each evidence candle and snapshot component;
- an immutable reference tying the evidence bundle to the published signal;
- outcome-time market state;
- an append-only signal lifecycle after publication.

The adapter also sets `decision_time` and `signal_emitted_at` from publication
time. That is useful transport metadata, but it is not a substitute for the
strategy's original decision timestamp.

### Terminal outcomes

The current strategy lifecycle mutates the economic position and emits
`TARGET_HIT` or `STOPPED` events. Setup invalidation is emitted as
`SETUP_INVALIDATED_BEFORE_ENTRY` or `INVALIDATED_NO_REENTRY`. The current V1
strategy does not implement `BREAKEVEN` or `TIME_EXIT` as authoritative
terminal states; `time_exit` is explicitly `NONE` in the frozen configuration.

The orchestrator adapter intentionally excludes already-terminal positions when
discovering new signals. Therefore terminal strategy events do not currently
become signal lifecycle events in `runtime/orchestration/events.jsonl`, and no
terminal outcome snapshot is attached to the canonical signal.

Phase 7 observes the economic position and persists V1 exit metadata plus
active-window observations. That provides useful research observation data,
but it is not an authoritative outcome-registration contract and does not
capture a decision-linked market snapshot at the terminal event.

### Persistence locations

Current persistence is split across:

- `context_structure_retrace_forward_state_compact.json`: compact strategy
  state, setup/opportunity/position lifecycle and selected provenance;
- `context_structure_retrace_forward.jsonl`: strategy lifecycle events;
- `runtime/orchestration/signals.jsonl`: canonical signal registrations;
- `runtime/orchestration/events.jsonl`: canonicalization, routing, and sizing
  events, but not terminal signal outcomes;
- `runtime/trade_manager/observation_stream/events.jsonl`: periodic active
  market observations and position updates;
- Phase 7 state/events: read-only observation lifecycle and threshold evidence;
- PostgreSQL migrations: normalized setup, opportunity, economic-position,
  lifecycle, and generic research-observation tables exist, but there is no
  dedicated decision-evidence bundle, signal lifecycle, or outcome-snapshot
  relation in the current contract.

## Identity audit

The existing identity chain is:

```text
setup_id
  -> entry_opportunity_id
  -> economic_position_id
  -> signal_id
  -> execution_intent_id / execution decision
```

`market_event_id` identifies the originating strategy event. The current
`StrategySignal` also carries `strategy_instance_id`, although the existing
adapters use fixed values such as `phase6` and `forward-paper`. Execution
records carry `signal_id`, `entry_opportunity_id`, and
`economic_position_id` where applicable.

This chain is sufficient for correlation, but signal lifecycle and evidence
references are not yet first-class links in the chain.

## Minimum additions for historically reproducible Signal Detail

These are the minimum contract additions before implementing Signal Detail.

### 1. Immutable strategy decision evidence

Strategy should publish one immutable evidence bundle per decision, containing:

- `evidence_id`, `setup_id`, `opportunity_id` when known, strategy instance;
- decision timestamp and source market timestamp;
- symbol/timeframe and the exact completed-candle window references;
- setup event, structure, levels/zones, indicators actually used;
- trigger/reason, intended entry, intended stop, all intended targets;
- invalidation criteria;
- strategy version, configuration identity, freeze identity, source code hash;
- source read health and evidence schema version;
- `future_data_used=false` and explicit causal cutoff.

The bundle may store normalized fields plus a canonical JSON payload, but it
must be immutable and content-addressed or otherwise hash-verifiable.

### 2. Decision-time market snapshot/window

Persist the exact market evidence used at decision time, including candle
identity, timeframe, OHLCV/spread fields, source timestamps, and snapshot
provenance. A reference from the decision evidence to this snapshot is
required. The snapshot must not be rebuilt from current broker history.

### 3. Durable orchestrator signal registration

Extend signal registration with `decision_evidence_id`, evidence hash, and
decision cutoff. Add an append-only lifecycle stream for registration,
publication, expiry, invalidation, and terminal transitions. The orchestrator
may validate references and route records, but must not compute indicators,
zones, triggers, or technical outcomes.

### 4. Active-signal market path

Persist ordered market observations from signal/entry through terminal outcome,
with explicit source timestamps and gap markers. This is the chart's active
path and must be separate from the decision snapshot.

### 5. Outcome registration and outcome snapshot

Create an authoritative outcome record keyed by `signal_id` and
`economic_position_id`, with:

- terminal reason and timestamp;
- outcome price/state as an economic observation;
- outcome-time market snapshot reference;
- source and authority of the outcome;
- link to the lifecycle event that registered it;
- separate broker-fill reference when a real fill exists.

`TARGET_HIT`, `STOP_HIT`, `BREAKEVEN`, `TIME_EXIT`, and `INVALIDATED` should be
represented as explicit terminal reasons, with strategy-specific reasons
preserved rather than collapsed into a generic status.

### 6. Explicit post-outcome boundary

Later candles may be stored for display context, but every post-outcome record
must carry an explicit `POST_OUTCOME_CONTEXT` classification and timestamp.
Signal Detail must never present that data inside the original decision
evidence or imply that it was available to the strategy.

### 7. Persistence model

The eventual normalized model should include equivalents of:

- `strategy.decision_evidence`;
- `research.decision_market_snapshots` and candle-window members;
- `orchestration.signal_lifecycle`;
- `research.active_signal_market_path`;
- `orchestration.signal_outcomes`;
- `research.outcome_market_snapshots`;
- explicit evidence/outcome references from `orchestration.signals`.

The existing JSONL stores can be the initial append-only transport, but the
same identity, immutability, hash, and causal-boundary rules must hold when
materialized into PostgreSQL.

## Non-negotiable invariants

- Strategy owns decision evidence; the orchestrator does not recalculate it.
- Decision evidence is captured before later candles exist in the signal's
  causal view.
- Active-path, outcome, and post-outcome data are separate evidence classes.
- A terminal outcome must carry an outcome-time snapshot or an explicit
  `SNAPSHOT_UNAVAILABLE` reason.
- Economic/strategy entries are not broker fills.
- Signal Detail reads persisted evidence and does not infer missing facts from
  current market data.
- Missing evidence is displayed as missing; it is never silently reconstructed.

## Audit conclusion

The current system has a useful identity chain, immutable canonical signal
objects, strategy lifecycle events, and a separate active-market observation
stream. It does not yet preserve a complete decision evidence bundle, does not
publish terminal outcomes into the signal lifecycle, and does not capture
outcome-time snapshots. The minimum additions above are therefore required for
historically reproducible Signal Detail.

