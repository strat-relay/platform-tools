# Console V1: Strategy Studio workflow specification

Screen/workflow specification only — no UI is built or designed visually here.  The
Console is the **internal operations console** (ADR-0001: never the customer portal); the
Studio is an internal authoring surface for the operator/trader.

## 1. Navigation

```
Strategies
  └─ <Strategy>                      overview: type, current draft lineage, versions, evidence summary
      └─ Studio
          ├─ Describe
          ├─ Examples
          ├─ Clarify
          ├─ Proposed Rules
          ├─ Decision Validation
          ├─ Replay / Backtest
          ├─ Decision Traces
          ├─ Versions
          └─ Promotion
Signals
  ├─ Candidates          (in-flight setups, AWAITING_REVIEW tasks)
  ├─ Generated           (EntrySignals, with trace link)
  ├─ Rejected            (rejected candidates, failing stage)
  └─ History
Research
  ├─ Backtests
  ├─ Evidence            (by provenance/class; forward clocks)
  └─ Validation sets     (pools, exposure ledger, consumption)
Registry
  ├─ Primitives          (versions, status, tests)
  └─ Reason codes
```

The existing Console already has Strategies, Signals, Trade Manager, Reports, Events and a
Controls page; Studio adds routes and resources, not a new app.  The trading ops API
(ADR-0001 §7) gains a `/trading/v1/studio/...` group with `author`/`reviewer`/`approver`
scopes; every write is audited.

## 2. Studio tab specifications

Common to every tab: a **header strip** showing draft lineage, current `definition_hash`
(short), authoring state (`DRAFT…BACKTESTED`), and a **staleness banner** when results on
screen belong to an older definition hash.

### 2.1 Describe
* **Purpose:** capture the strategy in words; choose the type intent (deterministic /
  hybrid / research-only) and instruments/timeframes in scope.
* **Shows:** free-text description (versioned), extracted terms and open ambiguities count,
  the AI analyst's restated understanding as **proposals** (never as rules).
* **Actions:** edit description; ask analyst to restate; set intended type; start clarification.
* **Guardrails:** analyst output is labelled "proposal"; nothing here edits the definition.

### 2.2 Examples
* **Shows:** example sets as a grid: thumbnail, label (POSITIVE / NEGATIVE / AMBIGUOUS),
  partition role, **evidence-kind badge** (`ANCHORED` / `VISUAL_ONLY`), hindsight-exposed
  warning, direction, instrument/timeframe.
* **Actions:** upload screenshot(s); set label; enter instrument/timeframe/decision time;
  annotate (entry/stop/target/zone/arrow/region/text); **anchor** (render canonical chart
  as-of, confirm "this is my picture"); accept/reject machine-proposed marks; write the
  reason.
* **Rules:** partition role chosen at creation and shown read-only afterwards; a
  `VISUAL_ONLY` example is visibly excluded from any metric; unanchored examples show why.
* **Empty/error states:** anchoring mismatch → keep as `VISUAL_ONLY` with the mismatch
  reason; unsupported image → reject with message.

### 2.3 Clarify
* **Shows:** three lanes — **Open ambiguities** (with the generated question), **Proposed
  interpretations** (patch preview + measured evidence + supporting/contradicting
  examples + `LOW_SAMPLE` warning), **Decided** (accepted/rejected with reasons).
* **Actions:** answer a question in words; accept / reject / edit an interpretation;
  **defer to review gate** ("I judge this by eye"); mark won't-define.
* **Guardrails:** an interpretation cannot be accepted without viewing its
  contradicting examples; accepting shows the exact definition diff; measured evidence
  is computed only on partitions the author may read.

### 2.4 Proposed Rules
* **Shows:** the current definition as a structured, readable rule tree (context → setup
  stages with windows → entry/stop/target → invalidation → review gates) with each rule's
  **origin** (which accepted interpretation/statement/example), the parameters table
  (value, bounds, tunable), compile status and the list of `COMPILE.*` findings.
* **Actions:** open a rule's origin; edit a parameter within bounds (creates a new revision);
  run compile; view canonical JSON and hash; diff two revisions.
* **Guardrails:** direct structural edits are limited to schema-valid forms; rules without an
  origin are highlighted; the mode banner states the consequence (e.g. "HYBRID: 1 review gate").

### 2.5 Decision Validation ("Would you take this trade?")
Layout (single-case focus):

```
┌───────────────────────────────────────────────────────────────────────────┐
│ Round 4 · Pool: UNTOUCHED (sealed) · Case 17 of 40          [masked]      │
├───────────────────────────────────────────────┬───────────────────────────┤
│  H1 context (ends at decision time)           │  Facts                    │
│  M15 structure (levels as-of)                 │   session · ATR · spread  │
│  M5 trigger                                   │   proposed geometry (if   │
│  ── nothing after decision time ──            │   a candidate is asked)   │
├───────────────────────────────────────────────┴───────────────────────────┤
│  ( YES )   ( NO )   ( NEED MORE CONTEXT )   reason ▾   note   annotate    │
│  [Show H4 as of decision time]  (logged)                                   │
└───────────────────────────────────────────────────────────────────────────┘
```
* **Modes:** `BLIND` (default; counts toward metrics) and `ASSISTED` (engine view shown,
  excluded from metrics, labelled).
* **After answering** (only for `VALIDATION_REFINEMENT` pool): reveal engine outcome,
  trace, blame; the case becomes *spent*.  In the untouched pool the reveal is withheld
  until freeze; only aggregates appear.
* **Round summary:** confusion table (§ of `04_…`), per-stratum counts with intervals,
  NEED_MORE_CONTEXT rate and top requests, consistency (planted repeats), and the **blame
  table** and **unexplained residue** lists linking each disagreement to its trace.
* **Guardrails:** no back-navigation to change a submitted answer (a correction is a new,
  logged answer); the page renders from the as-of snapshot service only.

### 2.6 Replay / Backtest
* **Shows:** run list (definition hash, partition, cost model, seed, status); per run the
  **attrition funnel** (counts passing each stage), candidates, signals, reference-trade
  results in R, and the evidence class label (`EXPLORATORY_DEVELOPMENT` etc., prominently).
* **Actions:** start a run on `DEVELOPMENT`; start a `ControlRun` (ablate roles); compare two
  runs; open any evaluation's trace.  The `HOLDOUT` run appears **only** for a `FROZEN`
  version and shows a one-shot confirmation ("this spends the holdout").
* **Guardrails:** exploratory results carry a permanent non-publishable badge; the number
  of configurations tried so far is displayed.

### 2.7 Decision Traces
* **Shows:** searchable list of Evaluations (filters: outcome, terminal reason, stage,
  instrument, time, validation case, run) and a **trace viewer**: ordered stages with
  PASS/FAIL/FLAG/UNKNOWN, observed vs threshold, margin, the exact bars used, state
  transitions, review answer, trace fidelity level (L0–L3), `trace_hash`, and a
  **"recompute" check** (recompute from inputs and confirm equality).
* **Actions:** open in chart at `as_of`; add to a validation set; copy machine-readable JSON.

### 2.8 Versions
* **Shows:** version list with status, `derived_from`, pins, engine version, freeze
  manifest, evidence summary by provenance class and a **forward clock**.  Compare two
  versions (definition diff, parameter diff, pin diff).
* **Actions:** freeze (from `BACKTESTED`) — opens a checklist (compile passes, pins
  approved, tests, validation policy on sealed cases, exposure declaration); create a
  derived draft from a version.

### 2.9 Promotion
* **Shows:** for the selected version, the current status, the next transition, and the
  **guard checklist** with live values against the `PromotionPolicy` (pass/fail per
  criterion, with the underlying numbers), plus history of `PromotionRecord`s.
* **Actions:** request/approve a transition (separate decisions, each with a reason);
  demote/retire.  Approval is disabled while any guard is failing unless an explicit,
  recorded waiver is entered for waivable criteria (holdout waiver only).

## 3. Signals and Research views (V1)

| View | Purpose | Key columns / features |
|---|---|---|
| **Candidates** | in-flight setups and HYBRID **review tasks** | strategy/version, instrument, direction, stage progress, countdown to gate timeout; **review action** here for `AWAITING_REVIEW` |
| **Generated** | EntrySignals | signal id, version, geometry, `evaluation_id` link → trace |
| **Rejected** | rejected candidates | failing stage + reason code, margin, trace link; attrition per stage |
| **History** | all of the above over time | filters by version/instrument; export |
| **Research → Backtests** | runs across strategies | class label, partition, cost model |
| **Research → Evidence** | evidence by provenance | BACKTEST / FORWARD / LIVE with forward clocks; superseded/withdrawn flags |
| **Research → Validation sets** | pools and exposure | pool state, consumed/sealed counts, exposure ledger, consumption per round |
| **Registry → Primitives** | primitive versions | status, tests, pinned-by count, withdrawn flags |

## 4. Cross-cutting behaviours

* **Roles:** `author` (edit, run on permitted partitions), `reviewer` (answer validation
  cases and review tasks), `approver` (freeze, promote).  V1 is one person holding all
  roles, but every action is attributed to a role and separately recorded.
* **Permissions:** sealed partitions are invisible to author-role queries in the API, not
  merely hidden in the UI.
* **Audit:** every state-changing action writes an audit event (DB) with actor, role,
  reason.
* **Freshness/staleness:** results tied to a different `definition_hash` are greyed with a
  "stale" label.
* **Data sources:** Studio pages read the trading DB through the Trading Ops API; charts
  are rendered from the as-of data service; **no page can query MT5, a broker, or any
  execution/subscription data** (constraint from the mission).
* **AI analyst panel** (Describe/Clarify/Examples): shows model id, what it was given, and
  that its outputs are proposals; can be disabled — the Studio must remain usable
  (manually authored definitions) with the analyst off.
