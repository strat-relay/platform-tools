# ADR-0001: StratRelay target system and domain architecture

* **Status:** Proposed (design only; nothing implemented)
* **Date:** 2026-09-21
* **Deciders:** Caleb (operator) — pending review
* **Supersedes:** the pre-product framing in `docs/ARCHITECTURE.md`; the auto-distribution
  route in `docs/DISTRIBUTION_PIPELINE.md`
* **Related:** A1 extraction manifest (`docs/extraction/`, commit `eda827c`);
  strategy ADR series `docs/ADR/ADR-001…020` (numbering is that series; architecture ADRs
  live in `docs/architecture/`); `docs/POSTGRES_SYSTEM_OF_RECORD.md`
* **Detail documents:** `01`–`08` in this directory; machine-readable map in `data/`

## 1. Context

**Product.**  StratRelay is a **signal subscription platform**.  Customers subscribe to
signal streams — **Strategy × Instrument** — priced by configurable combinations of
streams, delivery channels (Telegram, WhatsApp, SMS, Email, Portal) and optional premium
capabilities (notably the **Trade Manager Assistant**).  The public product shows
performance and history with mandatory provenance (BACKTEST / FORWARD / LIVE).
Testimonials are customer content, separate from measured performance.  V1 customers do
**not** connect broker accounts.

**How the code got here.**  The repository began as an MT5 research/paper-trading and
personal auto-execution system.  Consequently (all verified in source, `01_…`):

* the module named **`orchestration`** is really three things — signal canonicalisation,
  personal-account routing/sizing, and a placeholder distribution queue — in one process;
* the **Trade Manager** is already a pure, broker-free evaluator, but its package also
  holds account/ownership logic (`central.py`);
* **every canonical signal auto-enters a distribution queue**; there is no publication
  boundary, no `PublishedSignal`, no `ManagementSignal`;
* MT5 is embedded in strategies, Control API, the consumer and research through **seven
  hand-rolled clients**; there is no `MarketDataProvider` or `BrokerClient` abstraction;
* performance evidence exists as documents and ledgers, but there is no
  observation/series/snapshot model and no LIVE evidence ingestion;
* production state moves through **JSON/JSONL files and PID files**; PostgreSQL
  (offline foundation only) and NATS (core, JetStream disabled, shared with another
  product) are not used by trading;
* the "Console" is an internal operations tool; there is no public site or customer portal.

The A1 manifest plans the physical separation of `mt5-native-bridge` from
`trading-platform`.  This ADR fixes the **business/domain boundaries** so that
extraction lands on the product actually being built.

## 2. Decision drivers

1. The commercial platform must keep working with **personal MT5 execution fully disabled**.
2. Customer-visible facts must pass an **explicit publication boundary**.
3. **Trade Manager decisions are a product**, broker-independent, and must be
   distinguishable from raw entry performance without hindsight.
4. **Customer data, broker/account data and trading state must be separable** (privacy,
   blast radius, future compliance).
5. Reuse working structure; avoid a rewrite; avoid premature microservices.
6. Keep MT5, TradingView and any data vendor **replaceable**.

## 3. Decisions

### Product and domain shape
**D1.**  StratRelay is a signal-subscription product.  **Personal MT5 execution is an
optional consumer of trading decisions**, never a dependency of the product.

**D2.**  Five logical domains plus three surfaces: **A Trading Core**, **B Signal Commerce
(StratRelay application)**, **C Personal Execution**, **D Broker Integration
(`mt5-native-bridge`)**, **E Product surfaces** (public site, customer portal, operations
console).  Logical boundaries are decided first; deployment units and repositories follow
(D19).  Nothing is mandated to be a microservice.

**D3.**  **Trading Core** owns market-data and instrument abstractions, strategies and
versions, the signal lifecycle **including the Publication Gate**, the Trade Manager and
performance evidence.  It knows nothing about customers, subscriptions, distribution,
brokers or accounts.

**D4.**  **Signal Commerce** owns customers, subscriptions, entitlements, pricing,
publication *distribution*, notification preferences, the billing boundary,
testimonials and customer-facing performance read models.  It consumes **only published
events and contracts**; it never imports trading code.

**D5.**  **Personal Execution** (orchestration/authorisation, risk, execution intents,
reconciliation, ownership/fencing) is a **separate package with a one-way dependency on
Trading Core**, consuming `EntrySignal` and `TradeManagerDecision`.  Customer
auto-execution stays *architecturally possible* (another consumer of published events
behind a new context with its own credential vault) but is neither designed nor stubbed.

**D6.**  MT5 sits **beneath** the platform: `mt5-native-bridge` owns transport, broker
state/quotes/positions/orders/execution operations, EA and protocol, and **no policy**.
The platform reaches it only through adapters implementing the ports of D7.

### Abstractions
**D7.**  Explicit ports — **`MarketDataProvider`**, **`AnalysisProvider`**, **`BrokerClient`**
— with MT5 as the current implementation of the first and third.  **TradingView/Pine is
never a core dependency**: it may be an `AnalysisProvider` feeding strategies, and any
future canonical market data source (with appropriate licence) is a new
`MarketDataProvider`.  `Instrument` is canonical (`XAUUSD`) with per-provider symbol
mappings.

### Lifecycle
**D8.**  The lifecycle is explicit and typed: `StrategyOpportunity → EntrySignal →
PublicationDecision/PublishedSignal → ManagedTrade → TradeManagerDecision →
ManagementSignal → ClosedTrade → PerformanceObservation`, with `ExecutionIntent →
ExecutionResult` as a **separate branch**.  An internal strategy event never becomes a
customer signal without a `PublicationDecision`; a `TradeManagerDecision` never implies
execution.  **Distribution and execution are independent consumers** with no dependency on
each other.  The Publication Gate lives in Trading Core (Signals context) and is the sole
writer of published records.

**D9.**  **`SignalStream` = Strategy × Instrument**, with stable identity across versions.
A `StreamBinding` ties a stream over time to a `StrategyVersion`, a `ParameterSet` and a
`TradeManagerVersion` with role `PRIMARY | CHALLENGER | SHADOW`; only the primary
publishes.  A `ManagedTrade` **binds its StrategyVersion and TradeManagerVersion at open
and never rebinds**.  `ManagedTrade` is created for **every** `EntrySignal` of a stream in
`SHADOW` or later, so Trade Manager and FORWARD evidence exist before publication.

### Trade Manager as product
**D10.**  The Trade Manager is a **core product capability**, broker-independent.  It
manages a **reference trade** (signal geometry and reference fills) using prices from
`MarketDataProvider`, and emits `TradeManagerDecision`s in a canonical vocabulary
(`HOLD, MOVE_STOP, MOVE_TO_BREAKEVEN, TRAIL_STOP, PARTIAL_PROFIT, EXIT`).  Account
concepts (`account_context_id`, size, P&L, ownership, snapshot version) are removed from
the decision contract; the personal branch adds them in a **management translator**.

**D11.**  **Trade Manager Assistant is an entitlement that affects distribution and
visibility only.**  Decision generation, storage, performance and personal execution are
independent of entitlements; enforcement is server-side in Distribution and the Portal
API.  Trading Core contains no entitlement concept.

### Performance
**D12.**  Every trade closes with **two recorded outcomes**, `entry_only` and `managed`;
performance is published as separate **series kinds** `ENTRY_ONLY` and `ENTRY_PLUS_TM`,
never blended.  A **hindsight firewall** (freeze boundaries, emitted-decisions-only
replay, causality contract, immutable DB-timestamped decisions, counterfactuals
permanently `BACKTEST`, version pinning, publication anchoring, completeness invariant,
cost-model recording, supersession-not-mutation) makes reconstructed management
un-presentable as actual history.  Provenance is exactly `BACKTEST | FORWARD | LIVE`,
mapped from the repository's existing evidence classes.

**D13.**  **Trading Core computes performance; Commerce approves and publishes it.**
Commerce re-verifies an `evidence_hash` and can withdraw a publication automatically if
evidence is later reclassified invalid.  **Testimonials are a separate content type** with
no reference to any performance object and a table constraint that they are not measured
evidence.

### Data and transport
**D14.**  **Two logical PostgreSQL databases**, `trading` and `commerce`, one owner each,
separate roles, **no cross-database foreign keys or joins**; cross-references are opaque
ids by value.  Commerce stores its **own immutable read-model copies** of what customers
see.  Broker/account data lives only in the restricted `execution` schema; customer PII
only in `commerce.identity`.

**D15.**  **PostgreSQL is authoritative; NATS JetStream is the durable operational
transport**, using a transactional outbox and consumer inbox, at-least-once delivery with
idempotent consumers and per-key ordering.  **Commerce sends no events to Trading Core in
V1.**  `execution.*` events live in a separate NATS account invisible to Commerce.

**D16.**  **JSON/JSONL is not a production persistence system.**  Existing file IPC is
transitional with a named successor for each stream (`04_…` §5); JSONL/Parquet remain
research/export/backtest formats.

### Surfaces and APIs
**D17.**  Three surfaces, three APIs: **Public** (`/public/v1`, anonymous, cacheable, no live
signals), **Customer Portal** (`/portal/v1`, OIDC session, server-side entitlement
enforcement), **Operations** (Trading Ops API + Commerce Admin API behind one gateway,
Cloudflare Access + RBAC + audit).  The existing Console is **operations-only**.  The
Control API's read-only guarantee is preserved for the read plane; control actions live
in a separate, audited route group.  **Hostnames:** the previously configured
`api.stratrelay.app` for the Control API is reassigned to the customer-facing API; the
Control API moves to `ops-api.stratrelay.app` (nothing deployed yet).

### Repositories and extraction
**D18.**  Contracts (event schemas, OpenAPI) live in **`stratrelay-contracts`**;
`trading-platform` and `stratrelay-platform` **never import each other**.  Only
HTTP/NATS crosses.

**D19.**  **Fewest repositories that keep dependency rules enforceable:**
`mt5-native-bridge`, `trading-platform` (Trading Core + Personal Execution + trading ops
API + research, with strict packages), `stratrelay-platform` (commerce + distribution +
Portal/Public/Admin APIs), `stratrelay-ui` (site + portal), `stratrelay-console`
(renamed `trading-ops-console`), `stratrelay-contracts`.  `personal_execution` becomes its
own repository only on the triggers in `08_…` §1.

**D20.**  **Extraction sequence:** A1 Stage 0/1 (bridge leaves, byte-identical) proceeds
**unchanged**; A1 Stage 2 namespacing is **replaced** by the domain-aligned tree
(`core/{market,strategy,signals,trade_management,performance}`, `personal_execution`,
`adapters/mt5`, `ops_api`, `kernel`); the name `orchestration` is retired;
`signal_orchestrator.py` and `trade_manager/central.py` are split (`08_…` §3).

**D21.**  **Legacy identities are not renamed.**  `strategy_id` values that conflate
strategy, instrument, variant and version are embedded in state keys, dedupe
namespaces, provenance and hashes; new `strategy_key`, `stream_id` and `binding_id` are
introduced additively through a mapping.

**D22.**  Boundaries are protected by **architecture tests in CI** (`08_…` §4): core purity,
one-way dependencies, sole importer of the bridge client, no new production JSONL, event
privacy schemas, no cross-repo imports.

## 4. Consequences

**Positive.**  The commercial product no longer depends on MT5; Trade Manager becomes a
sellable, provable capability; performance claims become auditable; customer and broker
data are separated by construction; MT5/TradingView/data vendors are replaceable;
the extraction has a target that matches the business.

**Negative / costs.**  New capabilities must be built (Publication Gate, ManagedTrade
reference model, performance series, commerce platform, contracts, three UIs); the
`orchestration` and `central.py` splits touch the REAL execution path and must be
sequenced behind the A1 characterization tests; an ops hostname/config change is needed;
two databases and a contracts repository add operational surface.

**Neutral.**  A1 Stage 1 is unaffected.  Existing Postgres work is reused, with schema
placement adjusted before any writer exists.

## 5. Alternatives considered

| Alternative | Why rejected |
|---|---|
| Microservice per context now | operational cost without a scaling need; boundaries are not yet proven; modular monoliths with enforced packages keep the option |
| Commerce inside `trading-platform` | couples customer data and payments to the trading/broker runtime; violates D4 privacy and release-cadence separation |
| Trade Manager as part of personal execution | contradicts the product (customers buy management signals) and would tie Trade Manager to MT5 |
| Auto-distribute every canonical signal (today's `distribution_queue`) | no editorial/eligibility control; a replayed or stale internal event could reach customers |
| MT5 as the permanent market-data abstraction | locks the product to one broker feed, complicates licensing and future providers |
| JSONL as production persistence | no transactions, no constraints, O(n) dedupe, non-atomic checkpoints (verified in the code audit) |
| One PostgreSQL database / shared tables | erases ownership; makes PII and broker data joinable |
| TradingView as a core dependency | third-party availability/licensing risk on the critical path |
| Rename legacy strategy ids now | breaks frozen identity hashes and dedupe namespaces |

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Regulatory treatment of trading signals and performance advertising varies by jurisdiction | **Legal review before public launch (O11)**; disclosures as first-class content; claims workflow with evidence hash |
| Redistribution of prices derived from a broker feed may need a licence | decide canonical reference price source and licence posture (**O12**) before publishing levels externally |
| Fairness/ordering if personal trades precede customer delivery | decide and disclose (**O4**); optional publication delay policy in the gate |
| Performance is *reference*, not customers' realised results | mandatory labels; no P&L in account currency except LIVE with disclosure |
| Splitting the orchestrator/`central.py` on the REAL path | behind A1 T-C* tests; byte-identical Stage 1; separate approval per restart |
| Two-database consistency | outbox/inbox, deterministic ids, nightly reconciliation |
| Console/host rename churn | nothing deployed; single config change + image rebuild |

## 6b. Open decisions

Full list with recommendations: `README.md` §"Open decisions" (O1–O17).  Those that
gate **A1 Stage 2**: O2 publication authority, O3 XAU variants, O4 personal-execution
source event, O9 infrastructure tenancy.

## 7. Verification

* Architecture tests of `08_…` §4 run in CI from the first commit of the new packages.
* Contract tests (`stratrelay-contracts`) for every cross-domain event and API.
* A test that Trading Core has no reference to entitlement/subscription concepts, and a
  test that toggling an entitlement cannot change any `TradeManagerDecision` row.
* Parity runs (JSONL strangler gateway vs Postgres/NATS path) before any file stream is
  retired.
