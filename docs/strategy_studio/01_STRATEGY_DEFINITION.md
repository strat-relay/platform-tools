# StrategyDefinition model

Status: **proposed** (V1 design; nothing implemented).  Worked examples:
`examples/liquidity_displacement.v1.definition.yaml` and
`examples/htf_bias_retrace_engulf.v1.definition.yaml`, both linted for internal
consistency by `tools/check_examples.py`.

## 1. Four things that must not be conflated

| Object | What it is | Mutable? | Identity | Lives in |
|---|---|---|---|---|
| **Strategy** | the long-lived *idea* and container: name, purpose, owner, lineage of drafts and versions | metadata yes | `strategy_key` (`LIQUIDITY_DISPLACEMENT`) | `strategy.strategy` |
| **StrategyDefinition** | the **rules**: a content-addressed, immutable document (stages, features, entry/stop/target, invalidation, gates, parameter *declarations*) | never (a new edit is a new definition) | `definition_hash` = sha256 of canonical JSON | `strategy.definition` |
| **ParameterSet** | the **values** bound to a definition's declared parameters, optionally per instrument | never | `parameter_set_hash` | `strategy.parameter_set` |
| **StrategyVersion** | the **evidence-bearing release**: pins `definition_hash` + `parameter_set_hash` + exact primitive versions/implementation hashes + engine (runtime-semantics) version + freeze manifest + the partitions the authoring touched | identity never; only its lifecycle *status* advances | `strategy_version_id` = hash of the pins | `strategy.strategy_version` |

Consequences:

* **One definition, many parameter sets.** Today four configured "strategies"
  (`liquidity-xau-base`, `-xau33`, `-btc25`, `-usdjpy25`) are one rule set with four
  parameter sets (`examples/liquidity_displacement.v1.parameter_sets.yaml`): only
  `entry_fraction` (0.50 / ⅓ / 0.25 / 0.25) and `max_retrace_bars` (3 / 5 / 5 / 5)
  differ.  In the ADR-0001 model these are `StreamBinding`s of one `SignalStream` per
  instrument, each binding a `(StrategyVersion, ParameterSet)`.
* **A rule change is a new definition, hence a new version.**  A parameter *value*
  change is a new ParameterSet, hence also a new StrategyVersion (its identity hashes
  the pins), but it can share the definition and its validation history.
* **Draft revisions are not versions.**  While authoring, each edit produces an
  immutable `draft_revision` that points at a definition hash; a **StrategyVersion is
  created only at freeze** (`07_…`).
* **Metadata, evidence references and prose are excluded from `definition_hash`** so a
  typo fix in a description does not fork the version history; everything that can change
  a decision is inside it.

## 2. Strategy types and promotion

| `evaluation_mode` | Meaning | Runtime behaviour | May emit signals? |
|---|---|---|---|
| **DETERMINISTIC** | fully expressed by registered primitives; **no** review gates | evaluates without human or AI input | yes, autonomously |
| **HYBRID** | deterministic candidate generation plus ≥ 1 **declared** `review_gate` | evaluation pauses at the gate; the recorded human answer resumes it; **timeout ⇒ REJECT** | yes, only after gates pass |
| **RESEARCH_ONLY** | documented hypothesis + examples/evidence; may contain partial stages | surfaces `ResearchObservation`s / candidates for study; never a signal | **no** |

The type is a declared property of the *definition*, checked by the compiler
(DETERMINISTIC forbids gates, HYBRID requires them and requires each to be fail-closed,
RESEARCH_ONLY cannot reach a signal-emitting state).  It is **not** inferred.

### Promotion criteria (definition-level; distinct from version lifecycle in `07_…`)

Criteria are a versioned `PromotionPolicy` (thresholds are policy data, not constants
in this document) and are evaluated against **untouched** validation data only.

**RESEARCH_ONLY → HYBRID** requires all of:

1. every discretionary element is either an operational stage or an explicit
   `review_gate` (no free-text "and use judgement" left inside a stage);
2. the candidate generator **surfaces** the trader's positive examples: candidate recall
   on anchored `POSITIVE` examples ≥ policy minimum, with candidate volume bounded (the
   trader is not asked to review noise);
3. every review gate has a stated question, allowed answers, evidence shown, fail-closed
   timeout, and a recorded *why it is not yet operational* (an open ambiguity);
4. reviewer consistency: repeated planted cases receive the same answer at ≥ the policy
   rate (intra-rater reliability; V1 has one trader so inter-rater is not available).

**HYBRID → DETERMINISTIC**, per review gate, either:

* **replace** it by an operational predicate whose agreement with the human's blind
  answers on untouched cases meets policy **with disagreement analysis** (no unexplained
  cluster, per-stratum minimum counts), or
* **remove** it because an ablation shows it does not discriminate (the gate's answers
  are uncorrelated with the deterministic stages' outcomes and the human's own
  unaided decisions), recorded as a new version.

**Demotion** (DETERMINISTIC → HYBRID → RESEARCH_ONLY) is always allowed and creates a new
definition; it happens when validation drift, a withdrawn primitive, or forward evidence
shows the rules no longer reproduce the trader's judgement.

## 3. Definition sections

The canonical document has these sections; each answers one question.

| Section | Answers | Notes |
|---|---|---|
| `schema`, `strategy_key`, `evaluation_mode` | what is this | mode is declared |
| `meta` | how do humans describe it | **not hashed** |
| `direction` | long/short handling | `DETECTED` (a stage discovers it, e.g. the sweep side) or `EVALUATE_EACH` (mirrored evaluation) |
| `universe` | which instruments | `EXPLICIT`, or `PER_PARAMETER_SET` (instruments come from bindings) |
| `timeframes` | multi-timeframe hierarchy | **named roles** (`context`, `structure`, `trigger`) bound to concrete timeframes; everything else refers to roles, so re-basing M5/M15/H1 is a parameter change, not a rewrite |
| `data_requirements` | when is evaluation safe | minimum bars per role; below it ⇒ `INSUFFICIENT_DATA`, never a guess |
| `parameters` | tunable numbers | *declarations*: type, unit, default, **bounds**, `tunable` flag; values in a ParameterSet |
| `levels`, `features`, `derive` | reusable named inputs | registry primitives evaluated as-of a time; `derive` is a pure transform of stage outputs |
| `context` | market-state gates and flags before/around the setup | **gate** rejects; **flag** annotates only |
| `setup` | the pattern | `SEQUENCE` of stages (§4); `STATE_MACHINE` reserved for re-entry lifecycles |
| `filters` | session/time/other constraints | same gate/flag semantics |
| `entry`, `stop`, `target`, `exits` | reference-trade geometry | small expression language (§5) |
| `invalidation` | when the thesis is void | evaluated from a named stage until signal (and as an exit afterwards) |
| `review_gates` | declared discretionary confirmations | HYBRID only |
| `decision_policy` | how stage results become an outcome | reason precedence, when a signal is emitted |
| `evidence_refs` | example/validation sets consulted | **not hashed** |

## 4. Setups are sequences with bounded windows

The two live strategies, read independently, share one shape that a pile of boolean
conditions cannot express:

* **Liquidity Displacement** (`liquidity_displacement.py`): *sweep at bar i → reclaim →
  displacement **and** micro-break within 5 bars → retrace touch-and-hold within 3 bars*.
  The detector literally scans forward (`range(i + 1, i + 1 + N)`).
* The research liquidity sniper (`state_machine.py`) is the same idea with different
  windows: reclaim ≤ 2, displacement ≤ 5, BOS ≤ 2, and it already records a per-setup
  `trace` and a terminal reason (`RECLAIM_TIMEOUT`).
* **Context** (`context_structure_retrace_forward.py`) is a lifecycle:
  `SETUP_DETECTED → WAITING_FOR_RETRACE → FILLED | NO_RETRACE | INVALIDATED`, with a
  12-bar retrace window.

So the smallest useful model is **stages**, each `after` the previous, `within` a bounded
number of bars of a named role, with `select: FIRST`, plus `require` / `any_of`
predicates evaluated on the completing bar.

**Event time is explicit.**  Every stage produces an event with an `event_time` — *the
time it became knowable* (close of the completing bar).  The sequence's `decision_time`
is the `event_time` of its last stage.  This resolves the apparent contradiction that the
legacy `find_candidate(i)` "looks at bars after i": relative to the anchor bar it does;
relative to the decision it does not.  The compiler's lookahead analysis (`06_…` §3)
therefore reasons about **decision time**, not anchor bars.

Three legitimate uses of "future" data exist in today's code, and the model separates
them because only the first is forbidden in a decision path (evidence:
`data/strategy_code_inventory.csv`, 9 functions with forward scans):

| Kind | Example | Rule |
|---|---|---|
| **Outcome label** | `future_outcome_labels`, `measure_retracement` ("future path used for label only") | forbidden in evaluation; allowed only in evidence/analysis code |
| **Confirmation lag** | `confirmed_swings` (a pivot is known only `right` bars later; `known_at`) | allowed; the primitive declares `known_at` |
| **Sequence completion** | `find_candidate` scanning the displacement after the sweep | allowed; expressed as ordered stages with windows |

## 5. Expression model (deliberately tiny)

Not a programming language.  An expression is a **string over a fixed vocabulary**:

* literals; arithmetic `+ - * /`; comparison; `and or not`;
* `$name` — a declared parameter;
* `s.<stage>.<field>` — output of an **earlier** stage; `f.<feature>` /
  `f.<feature>@s.<stage>` — a feature, optionally evaluated as-of a stage's event time;
  `lv.<level>`; `d.<derived>`;
* context variables `market.spread`, `inst.tick_size`, `inst.min_stop_distance`,
  `direction`;
* **engine built-ins only:** `min max abs beyond toward dir close closes_through`.

No loops, recursion, user-defined functions, assignment, imports, I/O or randomness.
Anything richer must become a **registered primitive** (`02_…`), which is versioned,
tested and causality-declared.  This is the escape valve that keeps the language small.

## 6. Canonical form, hashing and the YAML hazard

* The **record** is canonical JSON: sorted keys, no comments, numbers normalised
  (no `1.0` vs `1`), strings NFC.  `definition_hash = sha256(canonical_json)`.
* YAML is an **authoring convenience only**, parsed by a strict YAML 1.2 loader and
  immediately canonicalised.  This is not cosmetic: while linting the examples,
  `on:` (a natural key name) and unquoted `YES` / `NO` were silently parsed as the
  booleans `True` / `False` by the common YAML 1.1 parser.  The schema therefore avoids
  such key names (`role:` not `on:`), answers are quoted, and the loader rejects
  implicit booleans outside boolean-typed fields.

## 7. Where each concern lives (to avoid strategy-specific code)

| Concern in the two examples | Model element | Strategy-specific code needed? |
|---|---|---|
| Sweep → displacement → retrace fill | `SEQUENCE` stages with windows | no |
| Session extremes as liquidity levels (UTC 08–21) | parameters of `liquidity.reference_levels@1` | no |
| Stop = sweep extreme ± max(0.10 ATR, 1.25 × spread, broker minimum) | `stop.price` expression | no |
| Four instrument variants | four ParameterSets | no |
| H1 bias, protected low, M15 support zone, M5 engulfing **or** rejection | context gate, `levels`, `any_of` | no |
| "Retracement too deep" | `invalidation` (`closes_through(context, lv.h1_protected)`), origin = accepted interpretation | no |
| "Is the H1 structure clean?" | declared `review_gate` (HYBRID) | no |
| A pattern nobody has defined yet | new primitive via the registry workflow | **once**, then reusable |
