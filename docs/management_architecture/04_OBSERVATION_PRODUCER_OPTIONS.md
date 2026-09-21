# 04 - Who should produce the canonical observation? (options)

Status: **RECOMMENDATION, not approved.** Answers A5 open decision **OD-A5-13**. The ADR is `21`.

## 1. Options

| Id | Option | Sketch |
|---|---|---|
| **A** | Strategy runner publishes observations | runners emit observation events for their own positions |
| **B** | Phase 7 observer remains producer | today's path, fixed and migrated |
| **C** | **Trade Observation Service (TOS)** in Trading Core (Trade Management domain) | a service that owns the set of open ManagedTrades' *observation needs*, pulls data through the `MarketDataProvider` port, persists and publishes trade-scoped observations |
| **D** | Trade Manager pulls observations itself | the evaluator calls providers directly (the shape of `CausalObservationCollector`) |
| **E** | Personal Execution publishes observations | the executor's broker-state loop becomes the source |
| **F** | *(source-derived)* two-tier: **instrument-level market snapshot** service + trade-scoped derivation in Trade Management | a shared snapshot per instrument per tick, referenced by trade observations |

## 2. Evidence that shapes the choice

* The sole producer today (Phase 7) is a **research observer** for one strategy, reading a **frozen legacy file**, emitting **no quote**, and is not in the intended stack (`M1`, `M2`, `M23`, A5 `P19`/`P27`).
* Strategy runners are **frozen** and must not be edited (A5 `08`/`09`); they produce strategy events, not market observations for other consumers.
* The execution consumer is the **only writer of broker state** and owns an account-specific, queue-contended path to the bridge (A5 `P8`, `01`); using it as the observation source would make the product depend on personal execution.
* Provider-agnostic collectors already exist in `trade_manager/` with injected `position_source` and `market_source` callables and are unused by production (`M17`, `M18`): the code base has already converged on "observation collection is a Trade Management concern behind injected providers".
* The Trade Manager evaluator is a pure function of (trade state, observation, frozen version) (`03`); production of the observation is a separate responsibility.
* A2 already places the loop `MarketDataProvider -> observation -> TM -> decision` inside the Trading Core, with `observation` in the `trade_management` schema (A2 `03` section 2, `05`).

## 3. Assessment

Legend: ✓ satisfies, ~ partly / needs care, ✗ fails.

| Criterion | A runner | B Phase 7 | **C TOS** | D TM pulls | E Personal Exec | F two-tier |
|---|---|---|---|---|---|---|
| Works without personal execution | ✓ | ✓ | ✓ | ✓ | **✗** | ✓ |
| Signals product works without MT5 writes | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| Future non-MT5 `MarketDataProvider` | ✗ (runner is MT5-bound) | ✗ (bridge tools hard-coded) | ✓ | ✓ | ✗ | ✓ |
| Deterministic replay | ✗ | ✗ | ✓ (historical provider, same code) | ~ (evaluator and fetch entangled) | ✗ | ✓ |
| FORWARD evidence (observation persisted, versioned, attributable) | ✗ (frozen runners cannot be extended) | ~ (research-scoped, no versions) | ✓ | ~ (nothing shared to attribute) | ✗ | ✓ |
| LIVE evidence | ✗ | ✗ | ~ (separate personal observation) | ~ | ✓ (but wrong layer) | ~ |
| Customer ManagementSignal generation | ✗ | ✗ | ✓ | ✓ | ✗ | ✓ |
| Ordering / idempotency | ✗ | ✗ (file order, sequence by appender) | ✓ (per-trade gapless seq, deterministic ids) | ~ | ✗ | ✓ |
| Failure isolation (producer failure does not corrupt decisions; decisions do not stall producer) | ~ | ~ (fail-open publish, silent loss) | ✓ | ✗ (one process, one failure domain) | ✗ | ✓ |
| Research / live parity | ✗ | ~ | ✓ (one derivation path) | ✓ | ✗ | ✓ |
| Account independence | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| Multiple TM versions on the same market facts (shadow/challenger) | ✗ | ✗ | ✓ | ✗ (each version re-fetches; facts diverge) | ✗ | ✓ |
| Bridge/EA load control | ✗ (runners each poll) | ✗ | ✓ (one poller per instrument, research listener) | ✗ (per consumer) | ✗ (execution queue) | ✓ |
| Operational complexity | low | low but broken | **medium** | low | medium | medium |

## 4. Why not the others

* **A** couples management to frozen strategy code and to MT5; a strategy cannot be extended to emit observations without touching frozen files.
* **B** would need a new quote source, a new position source (not the legacy file), a new trade identity, sequencing, versioning and a non-file transport - i.e. a rewrite - while remaining a Context-only research artefact with its own frozen hash (`phase7_source_hash`). Fixing it in place is more change than a clean service and preserves the wrong owner. Its research role (threshold/checkpoint measurements) is unrelated to producing product observations and can continue **as a consumer** of canonical observations.
* **D** is attractive because it exists in code (`CausalObservationCollector`). It fails on shared facts: two TM versions (shadow/challenger, derived versions with their own evidence clocks) must see **identical** observations to be comparable, and replay must be able to substitute the source without touching the evaluator. Pulling inside the evaluator entangles the two.
* **E** puts the product behind an account and a fenced execution path; it is the coupling A2 forbids.

## 5. Recommendation

**Option C, built as F's two-tier shape internally.**

* A **Trade Observation Service** in Trading Core (owner: Trade Management) is the *only* producer of `trade.observation.recorded`.
* Internally it records an **instrument-level `MarketSnapshot`** once per instrument per poll (dedupes provider load; referenced by every trade observation on that instrument) and derives **trade-scoped observations** for the open ManagedTrades of that instrument (`08`).
* It talks **only to the `MarketDataProvider` port** (`05`); today's adapter wraps the read-only `Mt5ReadClient` on the **research listener**, never the execution bridge.
* The evaluator (`TradeManager`) is a separate consumer of the observation stream (`10`, `11`); the two may be co-deployed at first but interact only through the stream and PostgreSQL.
* Phase 7 is **not** the producer. It is retired as a producer; its measurements stay research artefacts (KEEP) or become a consumer.

Decision status: **RECOMMENDED**, approval by Caleb/architect required. Sub-decision (bar-window transport) is `OD-A6-2`.
