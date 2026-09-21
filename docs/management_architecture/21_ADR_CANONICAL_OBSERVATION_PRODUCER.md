# ADR-0004 (proposed) - Canonical observation producer for Trade Management

| | |
|---|---|
| **Status** | **RECOMMENDED - not APPROVED.** Caleb / the architect must approve before any implementation. |
| Date | 2026-09-21 |
| Deciders | Caleb (owner/architect) |
| Answers | **A5 OD-A5-13**: *who should publish the canonical observation stream?* |
| Inputs | A2 `ADR-0001` (D7, D9-D12) and `docs/architecture/02-05`; A3 `26d4a40`; A4 `89ba8b2`; A5 `37321e5` (`docs/runtime_boundaries`); Codex P0/P1 `17c09b2`; evidence `tools/verify_management_evidence.py` (26 checks) |

## Context

The Trade Manager is a core StratRelay product capability, not personal-execution infrastructure. It must produce decisions and customer management signals when personal execution and MT5 execution are **off**, and must work against future non-MT5 providers and replay data.

Facts (source-verified, `01`):

1. The only producer of the observation stream the Trade Manager consumes is the **optional Phase 7 observer** (`M2`), which is not in the intended stack (`M23`), reads a legacy full-state file the runner declares immutable (A5 `P19`), is Context-only, carries its own frozen whole-file hash, and **publishes `bid=None, ask=None`** (`M1`).
2. The consumer discards every such observation (`M4`), is at-most-once (`M6`), resets eligibility on restart (`M7`), never accumulates MFE/MAE (`M13`), and computes decisions only in `REAL_MANAGEMENT` mode (`M8`).
3. The only registered policy has every action disabled (`M9`, `M25`); proposals would be rejected `POSITION_NOT_FOUND` (`M26`); REAL close/trail are refused by the bridge (`M24`); the executor supports 2 of 4 proposal actions (`M11`).
4. Pure, broker-free evaluation modules exist and a provider-agnostic collector pattern exists but is unused (`M17`, `M18`).
5. A2 already places the loop `MarketDataProvider -> observation -> TradeManager -> decision` in the Trading Core.

**Therefore there is no working producer to migrate.** The question is who should *own* the responsibility.

## Decision (recommended)

**The canonical observation producer is a new *Trade Observation Service* (TOS) inside Trade Management (Trading Core).**

1. **Owner of observation creation:** Trade Management (Trading Core). Nothing else publishes `trade.observation.recorded.v1`.
2. **Data source:** the `MarketDataProvider` port only. Today's adapter wraps the read-only `Mt5ReadClient` on the **research** listener (never the execution bridge); future providers and replay plug in behind the same port. `AnalysisProvider` (e.g. TradingView) never supplies a quote; `BrokerClient` never supplies observations.
3. **Shape:** trade-scoped observations per open ManagedTrade with gapless `observation_seq`, deterministic `observation_id`, market facts only, bars by digest reference; instrument-level `MarketSnapshot` records shared across trades (`02`, `08`).
4. **Persistence and transport:** PostgreSQL row committed with an outbox event in one transaction; JetStream `TRADING_OBSERVATION` (bounded, separate from domain events) is a *view*; the evaluator consumes with a durable, explicitly-acked consumer and an inbox (`09`, `10`).
5. **Phase 7 observer:** **not** the producer. It stops being a producer and is not edited (its hash is part of its identity); its research measurements remain research artefacts and may continue as a *consumer* of canonical observations.
6. **Strategy runners** do not publish observations (frozen; wrong coupling). **Personal Execution** does not publish observations (would couple the product to an account and to the fenced execution path). **The Trade Manager evaluator does not fetch data** (it consumes observations and resolves `bars_ref` through the port).
7. **The evaluator is a separate consumer** (may be co-deployed with the TOS initially) whose decisions are persisted before any effect (`11`); publication and execution are independent downstream effects (`12`, `13`).

### Direct answer to OD-A5-13

> The canonical observation stream is published by a **Trade Observation Service owned by Trade Management in the Trading Core**, fed by the `MarketDataProvider` port. The Phase 7 observer is retired as a producer; it is neither repaired nor migrated. There is no legacy stream to move to NATS: the canonical path is built and proven in shadow (P4.1-P4.4), then made authoritative for the product domain (P4.5), then the inert legacy transport is retired (P4.7).

## Alternatives considered

| Option | Verdict (details in `04`) |
|---|---|
| A. Strategy runner publishes | rejected: frozen code; per-runner MT5 coupling; cannot serve multiple TM versions |
| B. Phase 7 remains producer | rejected: wrong owner, missing quote, frozen legacy input, Context-only, unlisted process, own frozen hash |
| **C. Trade Observation Service in Trading Core** | **recommended** |
| D. Trade Manager pulls | rejected as the *producer*: entangles fetch and evaluate; no shared facts for shadow/challenger versions; no replay substitution. Its collector pattern is reused inside the TOS |
| E. Personal Execution publishes | rejected: violates "works with personal execution off" |
| F. two-tier snapshot + trade derivation | adopted **inside** C |

## Consequences

**Positive:** the product branch is independent of personal execution and of MT5 writes; deterministic replay and FORWARD evidence; multiple TM versions compare on identical facts; observation loss/duplication/reordering become detectable and repairable; per-trade fault isolation; no checkpoint file required for correctness.

**Costs / risks:**

* a new service and schema (`managed_trade`, `trade_observation`, `market_snapshot`, `trade_manager_version`, decisions, publication) and a `TRADING_OBSERVATION` stream;
* a `MarketDataProvider` adapter must exist (the read client is reusable; the port is not yet defined in code);
* provider load on the research listener must fit its EA queue budget (A5 `11` section 4) - measured in P4.3;
* `TradeManagerVersion` identity must be implemented (`07`); the first version is `TM-NONE` (audited `HOLD`s): **this ADR decides no management policy**;
* reference-feed vs execution-feed basis is a Personal Execution translation concern to be handled in P5 (`05`);
* the stack registry that lists the legacy `trade_manager` service lives in the **bridge** repository and must be edited there at P4.7.

## Non-goals

No Trade Manager policy change; no strategy runner change; no bridge change; no PostgreSQL/NATS change; no execution change; no broker-state authority change; no commerce/pricing design.

## Preconditions before implementation is scheduled

1. Approval of this ADR (`OD-A6-1`).
2. P2 handoff provides canonical EntrySignal identity/events (`20`); P0/P1 substrate available (`17c09b2`).
3. Runtime evidence checklist answered (`19`) - in particular whether an unlisted producer exists.
4. `OD-A6-2` (bar-window transport), `OD-A6-3` (challenger tracks), `OD-A6-4` (first TM version = `TM-NONE`), `OD-08` (retention/sizing) decided or scheduled.
5. P5 remains blocked by OD-06; nothing in this ADR relaxes that.

## Decision record (to be completed by the owner)

`Decision: ____   Date: ____   Conditions: ____   Alternative chosen if not C: ____`
