# ADR-0002: Strategy authoring is separate from strategy runtime; strategies are declarative, versioned definitions

* **Status:** Proposed (design only; nothing implemented)
* **Date:** 2026-09-21
* **Deciders:** Caleb (operator) — pending review
* **Related:** ADR-0001 (target architecture; D9 stream/bindings, D12 evidence, D14 databases,
  D15 events), A1 extraction manifest (`docs/extraction/`), strategy ADR series
  `docs/ADR/ADR-001…020`
* **Detail documents:** `docs/strategy_studio/00`–`11`; worked examples and linter in
  `docs/strategy_studio/examples`, `…/tools`

## 1. Context

V1 of StratRelay must let new strategies be **added through structured definition and
validation**, not by an engineer writing a new Python runner each time.  The trader
describes a strategy, supplies chart examples (would-take / would-not-take), the system
proposes rules and asks clarifying questions, a blind "would you take this trade?"
validation compares the engine with the trader, and a frozen, reproducible version enters
a PAPER / SHADOW / ACTIVE lifecycle.

What the repository looks like today (verified; `docs/strategy_studio/10_…`):

* Two live strategies plus research families, each **hand-written Python**, each with its
  own runner, state file, telemetry and reporting.
* Strategy identity is a **source-code hash plus config hash**; runners refuse to start if
  they change.  Legacy strategy files are therefore frozen.
* Both live strategies are, read independently, **time-ordered sequences of stages with
  bounded windows** (Liquidity: sweep → displacement + micro-break ≤ 5 bars → retrace fill
  ≤ 3 bars; the research sniper: reclaim ≤ 2, displacement ≤ 5, BOS ≤ 2) or a lifecycle
  (Context).  Only 27 of 315 functions take an `as_of` boundary; causality is by convention.
* "The same indicator" exists in **divergent implementations** (ATR with/without the current
  bar and over ranges instead of true ranges; strict vs non-strict pivots; two displacement
  definitions; an inline duplicate of rejection-wick logic; a dead `max(i-5, i-12)` operand).
* Decision telemetry exists only partly and is **duplicated logic** (`decision_telemetry`
  re-implements `find_candidate`).
* Research already practises chronological frozen splits, ablation controls, example cases
  marked `DIAGNOSTIC_ONLY`, and per-setup traces — informal versions of what is needed.

## 2. Decision drivers

1. A strategy that trades automatically must be **reproducible**: every decision resolves
   to the exact rules, parameters, primitives and engine that produced it.
2. An LLM may **help author** but must **never be a production trading rule**.
3. Vague human language must never silently become a deterministic rule.
4. Validation must be free of **outcome leakage, hindsight and authoring contamination**.
5. The rule model must be **small** (not a programming language) yet express future
   strategies, not only today's two.
6. Legacy strategies must keep running **untouched** while new ones are authored.
7. Studio must not depend on MT5, execution, subscriptions or distribution.

## 3. Decisions

**D1. Authoring and runtime are separate planes.**  Authoring (humans + AI Analyst) produces
a `StrategyDefinition`; runtime evaluates only a frozen, compiled definition.  The runtime
has no model calls, network, clock (other than `as_of`) or randomness.  The AI's output is
**proposals only**; only an authenticated human decision applies a change.

**D2. A strategy is a declarative, canonical, content-addressed document** made of named
timeframe roles, primitives (`id@version`), **sequences of bounded-window stages** with
explicit event times, context/filters with *gate* vs *flag* semantics, entry/stop/target/
exits and invalidation, declared review gates, and a **tiny expression vocabulary**.  No
loops, functions, assignment or I/O.  Anything richer is a **registered primitive**.

**D3. Four objects, never conflated:** `Strategy` (idea, mutable metadata) ·
`StrategyDefinition` (immutable rules, hash) · `ParameterSet` (immutable values, hash) ·
`StrategyVersion` (immutable evidence-bearing release pinning definition + parameters +
primitive versions/`impl_hash` + engine version).  One definition, many parameter sets (the
four Liquidity "strategies" are one definition with four ParameterSets).

**D4. Three explicit strategy types** — `DETERMINISTIC`, `HYBRID` (declared, fail-closed
review gates; performance labelled as system-plus-human), `RESEARCH_ONLY` (never emits
signals) — with promotion/demotion criteria expressed as versioned `PromotionPolicy` data
evaluated on untouched validation data.

**D5. Primitive registry with behavioural versioning.**  Any output-affecting change is a
new immutable version; strategies pin `id@version + impl_hash`; every primitive declares
`known_at` and passes golden, determinism, parity and **truncation-invariance** tests;
new ideas are **composite (declarative) first**, native code second; `EXPERIMENTAL`
primitives cannot be frozen into `PAPER`+ versions.  Divergent legacy implementations
become **separate versions**, never silently unified.

**D6. Compile to an `ExecutionPlan`, interpreted by a small versioned engine** (not direct
interpretation, not code generation).  Seven fail-closed validation passes; the plan and
`engine_version` are hash-pinned; live, forward and backtest share **one evaluation code
path**; runtime state is a rebuildable cache.

**D7. Time is explicit.**  Every stage has an `event_time` (when it became knowable); the
sequence's `decision_time` is its last stage's event time.  Three kinds of "future" data
are distinguished: **outcome labels** (forbidden in decisions), **confirmation lag**
(allowed, `known_at`), **sequence completion** (allowed as ordered stages).

**D8. Every evaluation yields a `DecisionTrace`** (stages with status, observed vs
threshold vs margin, evidence times, reason codes from a versioned registry, outcome,
`trace_hash`), reproducible from `(version, instrument, as_of, data snapshot)`.  Retention
tiers `FULL`/`SUMMARY`/`ON_DEMAND`; fidelity levels L0–L3 so legacy adapters are honest
about detail.  The trace is the single record for signals, rejections, backtests,
validation and (later) customer explanations.

**D9. Chart examples are teaching evidence unless anchored.**  An image is never market
data; `ANCHORED` examples are tied to canonical bars and verified by the trader;
`partition_role` is fixed at creation.

**D10. Clarification is explicit.**  Statements → terms → ambiguities → proposed
interpretations → recorded acceptance; every rule carries an `origin`; an unresolved
blocking ambiguity prevents `DETERMINISTIC`; the honest alternative is a declared review
gate.

**D11. Blind decision validation is the central V1 feature:** as-of snapshots only,
stratified sampling never conditioned on outcome, blind-to-engine by default, separate
`REFINEMENT` (spendable) and `UNTOUCHED` (sealed) pools, `NEED_MORE_CONTEXT` as its own
outcome, and **disagreement analysis** (blame by stage, reason ↔ rule map, clusters,
sensitivity, unexplained residue) instead of a single agreement percentage.
Outcome-linked analysis is allowed only on untouched sets after freeze.

**D12. Two lifecycles.**  *Authoring states* (`DRAFT → LEARNING → VALIDATING →
BACKTESTED`) live on a mutable draft lineage; *version states* (`FROZEN → PAPER → SHADOW →
ACTIVE → RETIRED`, with `ACTIVE → SHADOW` demotion) live on the immutable
`StrategyVersion` and only its status advances.  A version is created **at freeze**.
Any rule or parameter change after freeze creates a new version.

**D13. Dataset partitions with an exposure ledger:** `AUTHORING`, `DEVELOPMENT`,
`VALIDATION_REFINEMENT`, `VALIDATION_UNTOUCHED`, `HOLDOUT`, `FORWARD`.  Exposure is
monotonic; access is purpose-declared and enforced in the data layer; the **AI analyst
cannot read sealed partitions**; the holdout is **spend-once** per version with
multiple-testing disclosure; ablation `ControlRun`s are first-class.

**D14. Evidence integrity.**  Freeze starts the `FORWARD` clock; a derived version starts a
new clock and never inherits parent `FORWARD`/`LIVE` evidence; new classes
`EXPLORATORY_DEVELOPMENT` (never publishable), `UNTOUCHED_OOS` (holdout, once),
`POST_HOC_DERIVED`.

**D15. Legacy strategies run behind the same interface without modification.**
`LegacyStrategyRuntime` adapters (Liquidity: wrap the unmodified pure detector, **L2**;
Context: read-only event translator, **L1**) emit the canonical `Evaluation`; legacy
identities (`code_hash`, `decision_code_hash`, `config_hash`) are carried verbatim; a
declarative **twin** of Liquidity Displacement proves the model in shadow with a
divergence register; the research sniper families are the first declarative pilots.
No frozen file or hash is ever changed.

**D16. Placement and coupling.**  Studio lives in the Trading Core's Strategy context
(`trading` DB: schemas `strategy` (registry/runtime artefacts, mostly immutable) and
`studio` (authoring, append-only logs)).  It consumes only the `MarketDataProvider`
abstraction and produces definitions, versions, traces and evidence; it has **no**
dependency on MT5, brokers, personal execution, commerce, the Trade Manager or channels.

**D17. Events: six on JetStream** — `strategy.version.frozen`, `strategy.promoted`,
`strategy.primitive.withdrawn`, `strategy.candidate.detected`,
`strategy.review.completed`, `signal.entry.created`.  Everything else (draft/definition
revisions, examples, clarification, validation, backtests, and **every evaluation**) is a
database audit record.

**D18. Serialization.**  The record is **canonical JSON**; YAML is a front-end parsed by a
strict YAML 1.2 loader and immediately canonicalised.  Key names avoid YAML-1.1 hazards
(`role:` not `on:`), answers are quoted, implicit booleans are rejected outside boolean
fields.  (Found by linting the worked examples: `on:` and unquoted `YES`/`NO` silently
parsed as booleans.)

## 4. Consequences

**Positive.**  New strategies become data (plus, occasionally, a new primitive that is
then reusable); every decision is explainable and reproducible; hindsight and leakage are
structurally hard; legacy strategies are unaffected; one evaluation path removes
backtest/live divergence and duplicated telemetry.

**Negative / costs.**  A registry, compiler, engine, data service with sealed partitions,
a validation service and Studio screens must be built; primitives need parity work
against divergent legacy implementations; the DSL will not express Context's re-entry
lifecycle until a `STATE_MACHINE` kind is designed; single-reviewer V1 limits reliability
statistics to intra-rater.

## 5. Alternatives considered

| Alternative | Why rejected |
|---|---|
| **LLM evaluates each candidate at runtime** | non-reproducible, unauditable, latency/cost/availability risk; violates D1 |
| **Embed Python in definitions** | a strategy becomes arbitrary code; no static causality proof; unbounded audit surface |
| **Pine Script as the definition language / transpile** | third-party semantics and licensing on the critical path (ADR-0001 D7); TradingView remains *evidence/analysis input*, not the source of truth |
| **General rules engine / expression language (CEL, JsonLogic, Drools)** | still Turing-adjacent in practice, no notion of event time or bounded-window sequences; the stage model is the missing piece, the expression vocabulary is small enough to own |
| **ML classifier trained on the trader's examples** | opaque, needs far more data than a trader provides, cannot explain rejections; may still assist authoring |
| **Visual node graph as the canonical form** | good as an *editor* over the same canonical JSON; poor as the record (diffing, hashing, review) |
| **Interpret the definition directly at runtime** | validation and lookahead proof repeated or skipped; no stable pinned artefact |
| **Generate code from the definition** | see "embed Python" |
| **One combined lifecycle (DRAFT…ACTIVE on one object)** | permits editing after evidence exists |
| **Rewrite all legacy strategies first** | impossible under hash-frozen identities; unnecessary |

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Overfitting rules to a handful of examples | measured-evidence panels show `n`; untouched validation; spend-once holdout; multiple-testing disclosure |
| DSL cannot express a real strategy | primitive escape valve; composite primitives; reserved `STATE_MACHINE`; Context deliberately not first |
| Declarative twin diverges from legacy | golden corpus + **divergence register**; legacy remains the production identity |
| Primitive sprawl / near-duplicates | behavioural identity proven by parity; review on `APPROVED`; usage-count visibility |
| Proprietary strategy text/charts sent to a third-party model | decision **S2** (provider/data-handling); analyst is optional and can be disabled |
| Human reliability with a single reviewer | planted repeats measure intra-rater consistency; stated in every report |
| Validation data source differs from live feed | decision **S6**; `inputs_digest` records the data snapshot |
| Sealed-partition access leaks via tooling | role-level DB enforcement, not UI hiding; ledger on every read |

## 7. Open decisions

Recorded in `docs/strategy_studio/00_README.md` (S1–S14) with recommendations.  Those that
gate V1 implementation start: **S1** serialization/front-end, **S3–S4** holdout and
promotion thresholds, **S6** canonical historical data source; **S2** gates only the AI
Analyst work.

## 8. Verification (architecture tests)

* Studio and runtime packages import nothing from `personal_execution`, `adapters/mt5`,
  `stratrelay-*`, or any broker/MT5 module.
* The runtime package has no network/model-client dependency (import-linter + a test that
  it runs with sockets disabled).
* No API path lets an author-role query read sealed partitions (contract test on the ops API
  and on DB roles).
* Every primitive version passes the truncation-invariance and determinism tests in CI.
* A golden-trace corpus recomputes byte-identically for every claimed `engine_version`.
* No legacy strategy file, `code_hash`, `decision_code_hash` or `config_hash` changes as a
  result of any Studio work (CI compares the recorded hashes).
