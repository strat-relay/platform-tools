# Generic Strategy Backtest Core

Status: research/backtest infrastructure only. This package does not publish EntrySignals, change execution eligibility, access a broker, or alter live Context/Liquidity behavior.

## Boundary

`strategy_backtest` is the shared evaluation path:

```text
StrategyVersion -> registered StrategyEvaluator -> MarketEvent feed
                                      |                 |
                              historical feed      sequential/live feed
                                      v                 v
                                Backtest engine     future live adapter
```

The evaluator receives canonical completed `MarketEvent` values. It does not know whether an adapter obtained them from a historical dataset or a sequential live source. Provider symbols are feed provenance, never strategy identity.

## Evaluator contract

An evaluator implements:

- `initialize(strategy_version, parameter_set)`
- `consume_market_event(event)` returning `SetupLifecycleEvent` and/or `EntrySignal`
- `snapshot_state()`
- `restore_state(state)`

The `StrategyEvaluatorRegistry` resolves `StrategyVersion.evaluator_key` to an evaluator factory. The core has no strategy-name conditionals. Context, Liquidity, and future Kojo evaluators register adapters independently.

## Market and historical feed

`MarketEvent` carries canonical instrument, timeframe, open/close timestamps, OHLC, completed status, source, and provenance. `HistoricalMarketFeed`:

- rejects out-of-order events;
- excludes incomplete candles;
- applies explicit requested bounds;
- records a dataset/snapshot identity and `DISCOVERY` or `VALIDATION` partition;
- preserves chronological, deterministic iteration.

The core does not inspect future events while consuming the current event. The no-lookahead tests prove prefix invariance, future mutation isolation, completed-only input, and provenance availability timestamps.

## BacktestRun and artifacts

`BacktestRun` records StrategyVersion, evaluator fingerprint, immutable ParameterSet fingerprint, instruments, timeframes, dataset identity, requested range, partition, cost model, engine version, lifecycle status, and result fingerprint. `BacktestArtifactStore` writes a reproducible JSON result outside production PostgreSQL.

Statuses are `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and `CANCELLED`. Operational timestamps are excluded from the result fingerprint.

## Parameter sets

`ParameterSchema` validates required fields, enum values, and bounds. `ParameterSet` has canonical serialization and a deterministic fingerprint. `ParameterSetCatalog.mark_authoritative()` freezes an id to its fingerprint; a later replacement with the same id is rejected. Existing code-owned parameters remain compatible because the catalog is additive and no production parameters are edited here.

## Simulated execution

`SimulatedExecution` is broker-independent and accepts `MARKET`, `LIMIT`, and `STOP` signals. V1 acceptance requires MARKET and includes the other order types as deterministic extension points. It models entry, stop, target, expiry/time exit, explicit spread/commission costs, and realized R.

Same-bar stop/target ambiguity uses the recorded conservative policy `CONSERVATIVE_STOP_FIRST`; a candle touching both cannot silently become a profitable result.

No bridge, MT5 endpoint, broker request, or execution authority is used.

## Metrics and provenance

The generic metric block includes signal/trade counts, wins/losses, win rate, gross/net R, expectancy, profit factor, maximum drawdown, average R, median R, and breakdowns by instrument and direction. Timeframe, session, regime, and parameter-set breakdowns remain extension points.

Result artifacts contain the generated signals, outcomes, metrics, same-bar policy, and run provenance. Identical evaluator/feed/parameter/cost inputs produce identical result fingerprints.

## Discovery and untouched validation

`DISCOVERY` and `VALIDATION` are explicit feed partitions. The core does not optimize or rank parameter sets, and no automatic promotion is provided. A future validation service must reject selection workflows that use the untouched validation partition for discovery decisions.

## Parity harness

`assert_live_replay_parity()` runs the same registered evaluator over equivalent historical and sequential feeds and compares setup/signal payloads. It is the onboarding gate for future strategies. The feed adapters may differ; the evaluator and ParameterSet must be identical.

## Onboarding lifecycle

```text
DRAFT -> FORMALIZED -> IMPLEMENTED -> BACKTESTED -> VALIDATED -> FROZEN
                                                        |
                                               SHADOW / ONLINE
                                                        |
                                      ACTIVE / execution eligibility separately
```

ONLINE is not execution eligibility. The core does not create StrategyInstances, memberships, risk allow-list entries, or execution routes.

## Kojo next step

`KOJO_WEDGE_V1` is the first intended consumer, but no Kojo evaluator is implemented here. Its resolved geometry, breakout, entry, stop, target, lifecycle, and XAUUSD/H1 scope decisions must be imported from the authoritative Kojo formalization artifacts and registered as a normal StrategyVersion evaluator in a follow-up change. The generic core must not infer missing rules or borrow Liquidity/DOL semantics.

## Existing strategy adoption gaps

- Context: adapt its causal replay/evaluator to emit the generic `MarketEvent`/output contract and register a StrategyVersion; keep current runtime path unchanged until parity is proven.
- Liquidity: adapt its completed-bar stateful evaluator and existing parity fixtures to the registry/feed contract; keep its live 22347 boundary and runtime activation controls unchanged.
- Both: add explicit ParameterSet provenance and result artifacts before any framework-backed promotion workflow.
