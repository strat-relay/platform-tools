# 10 - A4 open decisions OD-01 to OD-04: what the repositories can and cannot say

Method: static only. Platform `88528e6`; bridge repo commit `5d4b018` read via git objects (it also holds the stack supervisor definition `scripts/mt5_stack_services.json` and the EA). **No production runtime directory, log, database or stream was inspected**, so nothing here claims to know what is *running*; where a question is runtime-dependent it says so. Checks `P9`-`P12`, `P19`, `P21`, `P22` in `tools/verify_evidence.py` re-verify the cited facts.

## Intended deployment, as defined in the repository

`scripts/mt5_stack_services.json` (bridge repo, `5d4b018`) is the stack registry. It defines 13 services:

| Service | Command (abridged) | Mode |
|---|---|---|
| `execution_mt5` | MetaTrader 5, execution Wine prefix | `REAL_EXECUTION` |
| `execution_bridge_22348` | `execution_bridge.py` (launchd) | `EXECUTION_ONLY` |
| `real_consumer` | `live_execution_consumer.py start --interval 15` | `REAL_EXECUTION` |
| `trade_manager` | `python3 -m trade_manager.stream_consumer start --mode REAL_MANAGEMENT --interval 15 ...` | `REAL_MANAGEMENT` |
| `orchestrator` | **`signal_orchestrator.py real-start --interval 15`** | `REAL_EXECUTION` |
| `context_runner` | `context_structure_retrace_forward.py start --interval 15 --symbols XAUUSDm BTCUSDm USDJPYm EURUSDm` | paper/shadow producer |
| `liquidity_base_runner` | `liquidity_displacement_forward.py start` | paper producer |
| `liquidity_xau33_runner`, `liquidity_btc25_runner`, `liquidity_usdjpy25_runner` | `liquidity_displacement_entry_forward.py <name> start` | `ACTIVE_REAL per platform config` |
| `research_mt5`, `research_bridge_22350` | research terminal / bridge | read-only |
| `liquidity_live_observer` | `scripts/watch_liquidity_live_all.py` | read-only, optional |

This is *definition*, not proof of what is running. It is the strongest static statement of intent available.

---

## OD-01 - `tradeability_decisions` / `OrchestrationStore.paths`

**Status: `CONFIRMED_LIVE_DEFECT` at deployment-definition level** (reachable on the committed configuration in the intended launch mode; reproduced). Whether the *running* orchestrator process has loaded this code is `UNKNOWN_RUNTIME_DEPENDENT`.

### Call chain and reachability

```
signal_orchestrator.py real-start                      (stack registry: mode REAL_EXECUTION)
  -> run() -> poll_once(store, config, mf, "REAL_EXECUTION")
       -> route_signal(store, signal, config, provider, "REAL_EXECUTION")      signal_orchestrator.py:146
            for portfolio, account in registry.routes_for(strategy_id):
               account_allowed = account["execution_mode"] == orchestration_mode
               ... if not account_allowed: REJECTED route; continue              (SHADOW mode would stop here with this config)
               try:
                  raw = provider.account_snapshot(...); ...quote, metadata...
                  tradeability = evaluate_tradeability(...)
                  store.append("tradeability_decisions", ...)                    signal_orchestrator.py:214   <- KeyError
               except Exception:  -> SKIPPED sizing row, event SIZING_SKIPPED    signal_orchestrator.py:248
```

* `orchestration/config/platform.json`: `execution_mode: REAL_EXECUTION`; account `exness-shadow-1` `execution_mode: REAL_EXECUTION`, enabled; portfolios for Context and the Liquidity family enabled for that account. With `real-start` the guard `account_allowed` is true, so line 214 is reached for every routed signal (`P21`).
* `OrchestrationStore.__init__` declares eight streams; `tradeability_decisions` is not among them (`orchestration/storage.py:13`), and no code adds it (`git grep`: one occurrence in source, the writer, `P9`).
* History: the writer arrived in bridge commit `c6482c3` ("Checkpoint production worktree changes", 2026-09-20); `storage.py` never listed the stream (`git log -S`).

### Behaviour (reproduced, `P22`)

With a provider that can return a quote, `route_signal` yields `decision=SKIPPED`, `reason=MISSING_ACCOUNT_DATA`, `error="'tradeability_decisions'"`. The exception is **swallowed inside `route_signal`** (inner `except`), so:

| Effect | Result |
|---|---|
| `tradeability_decisions.jsonl` | never created |
| `sizing_decisions.jsonl` | one `SKIPPED/MISSING_ACCOUNT_DATA` row per (signal, account); event `SIZING_SKIPPED` |
| `delivery_status` | **not** `ROUTE_DEGRADED` - `route_signal` returns normally, so `DELIVERY_COMPLETE` is written. (A4 suggested looking for `ROUTE_DEGRADED`; that is the wrong discriminator. Use the `error` string above.) |
| orchestrator-side REAL tradeability gate (`REAL_TRADEABILITY_BLOCKED`, rejected sizing) | **never executes** |
| REAL execution intents | **unaffected**: the consumer's REAL branch does not read `sizing_decisions` nor `tradeability_decisions` (it sizes from signals + its own risk policy, `P11`). The tradeability policy therefore has **no enforcement path into execution even if the append were fixed** |

Safety classification: the defect does not stop REAL execution and does not add exposure; it silences a policy check and the analytical sizing ledger (the check is a no-op). It is a **policy-bypass finding**, not an execution outage.

### Why it was not noticed

`test_signal_orchestrator.test_distribution_is_independent_of_sizing` asserts only `len(sizing_decisions) > 0` (a `SKIPPED` row satisfies it) and its `FakeProvider` has no `quote`, so the exception fires even earlier; `tests/test_tradeability.py` tests `evaluate` only.

### Runtime discriminators (for the owner; not collected here)

`sizing_decisions.jsonl` rows with `error == "'tradeability_decisions'"`; absence of `tradeability_decisions.jsonl` in the runtime orchestration directory; `SIZING_SKIPPED` events. If absent, the running process predates `c6482c3` and a restart would activate the defect.

### Migration implication

P2 reconciliation must model the *current* output (SKIPPED rows) and classify them `KNOWN_LEGACY_DEFECT`; nothing may fix it silently. A fix is a separate decision with a behaviour question attached (enabling the gate would begin applying a policy that has never been applied in REAL; and it would still not reach the consumer).

---

## OD-02 - `real_execution_resume_generations/`

**Status: `UNKNOWN` (existence) / no source contract - `ORPHANED` if it exists.**

* Exact string `real_execution_resume_generations`: **no match** in any tracked file of the current tree, in the platform history (`git log -S`), or in any commit reachable in the bridge repository (`git log --all -S`) (`P10`).
* What does exist: the file `runtime/execution/real_execution_resume.json`, written only by `establish_execution_resume_cutoff` (`live_execution_consumer.py:325-356`, tmp + `os.replace`), holding `real_execution_resume_generation` (integer, `+1` per operator action), `real_execution_resumed_at`, `excluded_signal_ids`.
* **A second, unrelated generation counter**: `live_execution_resume_generation` in `orchestration/state.json`. No production source writes it; only `control_api/app.py:363` reads it (to flag `GENERATION_MISMATCH`) (`P12`). It can only be non-empty if something outside this repository writes it.
* `execution_state_consistency()` (`:200-237`) requires four artifacts to agree (manifest, orchestration state, `real_state.json`, resume file) but only checks the resume generation for being non-zero (`:228`). **No generation is attached to intents or sends** (`01` row 1).

Conclusion: nothing in source reads or writes a `..._generations/` directory. If it exists in a runtime directory it is operator-created or historical. Treat as an import-as-history artifact at P5 only if found by a read-only look.

---

## OD-03 - Is the Phase 7 observer part of the intended deployment?

**Status: `OPTIONAL_NOT_IN_INTENDED_DEPLOYMENT`; live status `UNKNOWN_RUNTIME_DEPENDENT`.**

* `docs/OPERATIONS.md`: "Phase 7 is optional and passive ... Start it only after its manifest exists."
* The stack registry (13 services above) contains **no** Phase 7 observer; no reference in `compose.yaml`, `deploy/`, `scripts/` or the launchd plist.
* `control_api/observability.py:149,160-163` integrates it if present (report builder `CONTEXT_STRUCTURE_RETRACE_V1_PHASE7_OBSERVER`).
* The observer reads `PHASE6_STATE = context_structure_retrace_forward_state.json` (the **legacy full** state), which the Context runner declares "immutable after cutover" (`P19`): if the observer runs, it observes a frozen snapshot.

**Producer-of-record finding (`P27`).** The observation stream consumed by the Trade Manager (`trade_manager/stream_consumer.py`, `FanoutConsumer("trade_manager", ...)`) has **exactly one publisher in the repository: the Phase 7 observer** (`context_structure_retrace_phase7_observer.py:35,276,281`). So the optional observer is the *sole upstream* of the `trade_manager` service that the stack registry lists in `REAL_MANAGEMENT` mode, and it enumerates positions from the legacy full-state file the Context runner treats as immutable. Reading the repository alone, one of three things is true: (i) the observer runs and observes a frozen position set, (ii) it does not run and the Trade Manager receives nothing, or (iii) an unlisted producer exists. Combined with the bridge refusing REAL close/trailing (`B4`), **the REAL management chain is inert at this baseline on two independent counts**, whichever it is. Which of (i)-(iii) holds is `UNKNOWN_RUNTIME_DEPENDENT`.

Conclusion: not a listed service, yet load-bearing for the Trade Manager's input. Decision for the owner (OD-A5-13): declare the observation producer (retire the observer and specify its replacement, or re-point it at the compact state). This does not block P2 or P3; it determines what P4 (observation fan-out, management) actually migrates.

---

## OD-04 - Is demo mode still a supported runtime mode?

| | Status | Evidence |
|---|---|---|
| **Code support** | **YES** | consumer CLI `demo-audit`, `demo-arm`, `demo-status`, `demo-disarm`; `run()` selects `DEMO_EXECUTION` if demo state is armed and REAL is not (`:1938`); `DemoExecutionAdapter` mode `DEMO_EXECUTION`; bridge accepts `mt5_market_order` only for `DEMO_EXECUTION` (`B4`); `platform.json` `demo_*` keys; `docs/DEMO_EXECUTION.md` (which says `demo-arm` refuses until the transport is verified and geometry approved); tests |
| **Deployment** | **NOT in the intended deployment definition; actual `UNKNOWN`** | the stack registry has no demo service and sets `required_mode: REAL_EXECUTION` on the execution stack; `platform.json` `execution_mode` is `REAL_EXECUTION`. Code support must not be read as deployment. |

Consequence for migration: `EXE-13 demo_*` artifacts stay `RETIRE`-candidates (A4) but retirement needs the owner's statement that demo is not used; the DB design does not need a demo path unless the owner wants one (`DEMO_EXECUTION` uses the legacy `mt5_market_order`, which ignores the idempotency key).

---

## Corrections this analysis makes to A4 (A4 is not modified)

| A4 statement | Correction |
|---|---|
| `ORC-04`: readers "control_api" | **no reader exists** (`P9`) |
| `ORC-03` sizing_decisions: consumer `create_intents` reads it | only the non-REAL branches do; the REAL branch does not (`P11`) |
| `MGT-01` ownership registry: `OUTBOX_PROJECTOR` at P4 | its **only writer is the execution consumer** (`P7`); it cannot be an outbox/projector migration until the consumer moves (P5) |
| `MGT-02` broker_state: single new publisher at P3 | the current writer is the execution consumer's loop (`P8`); same coupling |
| A4 docs 06/08: "idempotency key honoured by the bridge" | **false at baseline** (`B1`, `B2`); it is a validated-and-ignored field |
| A4 `OD-01` discriminator `ROUTE_DEGRADED` | wrong; use the `error` string |
| Bridge lifecycle file as reconciliation source | it is unindexed by key and is a **bridge-private** file; the platform must not read it (A4 `EXE-15`) - replaced by `write-status` |
| "`contracts/mt5_bridge` client is the write path" (implicit in A1/A4) | not used by the consumer (`P1`) |
