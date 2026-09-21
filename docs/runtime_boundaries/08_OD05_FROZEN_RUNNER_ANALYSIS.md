# 08 - OD-05: what "frozen" actually freezes, and what a runner's file output is

Question inherited from A4: how do frozen strategy runners stop depending on production service-to-service file IPC without changing frozen strategy logic or source hashes, so that `PRODUCTION_FILE_IPC=0` has no permanent runner exception?

## 1. What is enforced, per runner (source evidence)

| | Context (`context_structure_retrace_forward.py`) | Liquidity (`liquidity_displacement_forward.py` + `liquidity_displacement.py`) | Phase 7 observer |
|---|---|---|---|
| Identity enforced at start | `assert_frozen()`: manifest `configuration_hash` == `config_hash()`; `phase2_representation_hash` == literal `PHASE2_HASH`; **`decision_code_hash()` == `FROZEN_DECISION_CODE_HASH`**, computed with `inspect.getsource` over exactly five functions: `_geometry`, `make_setup`, `_fill`, `_process_bar`, `process_symbol` (`P16`) | `source_hash()` = sha256 of `liquidity_displacement.py` bytes; refuses to run if it differs from `source_sha256_at_start` (`main`, line ~529) | own whole-file `phase7_source_hash()` recorded in its manifest |
| Whole-file hash of the runner | **not enforced**: `code_hash` in the manifest is a legacy constant that "predates observability-only persistence"; the guard was deliberately narrowed to the decision fingerprint (`P17`) | `runner_sha256` is *recorded* in instance manifests and **never compared** (`P18`) | enforced (whole file) |
| Persistence edited after freeze | **yes, verified (`P26`)**: four commits after the initial file (`80e558e`, `edff21b`, `868f36f`, `c6482c3`, bridge repo history) edited the runner (state projection, event identity, recovery metadata, ...) and the five-function decision fingerprint, recomputed at each commit, is **identical** to `FROZEN_DECISION_CODE_HASH` throughout | n/a (runner was extended around an unchanged strategy module) | n/a |
| Extraction policy | Stage 1 kept bytes identical (`docs/LEGACY_STRATEGY_PROVENANCE.md`); "Stage 2 namespacing requires an explicit re-freeze because path-sensitive identity may change" | same | same |

**Conclusion 1.** *Enforcement* freezes the **decision code** (Context: five functions; Liquidity: the strategy module), not the runner's persistence or output code. *Policy* (Stage 1) froze all bytes to keep provenance simple, and says a later stage may re-freeze. Persistence code has already been edited after freeze without touching identity; the code says so in a comment (`assert_frozen`).

**Conclusion 2 (an identity gap).** Liquidity's *signal shaping* - `detect()` (setup id `LDSV1-...`, geometry, target from `target_r`, spread/realistic-fill arithmetic), `signal_features`, `decision_telemetry` - lives in the **runner**, outside the hashed module, and is not fingerprinted anywhere. The safest reading is therefore that the whole runner module is behaviourally frozen even though only the strategy module is hash-enforced. Any approach that leaves those functions byte-identical avoids the question.

## 2. Separating the four concerns (actual code)

| Concern | Context | Liquidity |
|---|---|---|
| **Decision logic** | `_geometry`, `make_setup`, `_fill`, `_process_bar`, `process_symbol` and pure helpers (`_context`, `_spread`, `_m5_mechanisms`, `_event_id`) | `LiquidityDisplacementStrategy.evaluate` (`liquidity_displacement.py`); plus `detect`/`process` shaping in the runner |
| **Runtime wrapper** | `poll`, `read_symbol`, `_recovery_context`, `_finish_recovery`, `_tag_recovery_lineage`, `persist_new_opportunity_provenance`, `run` | `read_once`, `signal_features`, `detect_gap`/`replay_gap`, `process`, `checkpoint`, `main` loop |
| **Persistence / output** | `atomic_json`, `load_state`, `save_state`, `_event_identity`, `_event_already_written`, `append_event`, `write_heartbeat`, `_persist_setup`, reports | `atomic_write`, `save`, `event`, `load_state`, `write_heartbeat`, `persist_daily`, `write_summary` |
| **Process lifecycle** | `acquire_lock`/`release_lock` (PID file), `STOP_FILE` `/tmp/context-structure-retrace-v1-paper.stop`, `main`, `run` | `acquire_lock`/`release_lock`, `STOP` `/tmp/...stop`, `main` |
| **Data input** | bridge reads over HTTP (`bridge_read` -> `call_bridge`) on the research listener | `read_once()`: a **subprocess** (`wine ... mt5_read_once.py`) whose stdout is parsed - not the bridge, not a shared file |

## 3. The seams that already exist

1. **The frozen Context decision functions perform no file I/O themselves.** Checked by AST (`P23`): among the IO-related calls, they reach persistence only through the module-level `append_event` and `_persist_setup`, which are *outside* the fingerprint.
2. **The Liquidity instances already reuse the unmodified base runner by reassigning its module-level IO constants** (`STATE`, `EVENTS`, `DAILY`, `SUMMARY`, `MANIFEST`, `PIDFILE`, `HEARTBEAT`, `STOP`) - `liquidity_displacement_entry_forward.py:62-71` (`P24`). Rebinding module globals is therefore an established, working technique in this codebase.
3. **V1.1's `LiquidityLegacyRuntime` already calls the unmodified `LiquidityDisplacementStrategy.evaluate`** and produces an `Evaluation` (L2). The Context adapter translates lifecycle records (L1).
4. **`append_event` embeds behaviour** (recovery tagging, identity dedupe, `state["counters"]["events"]`, checkpoint) before the byte write (`P25`). A wrapper must therefore sink *bytes* below it (`EVENTS`, `atomic_json`), not replace the function, or it re-implements frozen-adjacent behaviour.

Consequence: **the frozen decision logic can be invoked without editing any source file**, provided the wrapper rebinds the byte-sink seams. Behaviour identity then depends on the wrapper reproducing the sink contract exactly (`atomic_json` replaces atomically; `EVENTS.open("a")` appends line-atomically; `state` is mutated in place, `save_state` keeps object identity).

## 4. Options

Legend for "file IPC reaches zero": the A4 definition (`docs/migration/17`) forbids file service-to-service transport, file authority, and any **checkpoint file required for correctness**. A runner's state file is required for its own correctness after restart (Context: `load_state` refuses to start without it), so *private* runner state on local disk is only compatible with the definition if it is treated as the runner's durable store - which is exactly what a container/PVC deployment or "no local disk" target rules out. This analysis treats literal zero as: **no runner state/output file is read by any other process and none is required by the runner to restart**.

| | **A** keep runner, permanent file output | **B** adapter in-process around unmodified runner; runner keeps private files | **C** new runtime wrapper (StrategyHost) calls frozen code, persists to PostgreSQL | **D** runner as subprocess with stdout/pipe/socket | **E** behaviour-identical new StrategyVersion, re-frozen |
|---|---|---|---|---|---|
| Strategy source/hash | untouched | untouched | **untouched** (wrapper is a new file) | needs runner edits **or** interception of file syscalls | new identity |
| Behaviour identity | identical (nothing changes) | identical if the hook is passive | identical *if* the sink seams reproduce the contract; **proved by golden replay**, not assumed | identical only if intercepted below the runner; edits break policy | must be *proved* equivalent; two identities co-exist until then |
| Operational complexity | lowest (today) | medium: new entrypoint hosting runner loop | medium-high: new host, DB state, leases, parity harness | high: FIFO/tmpfs/socket is a file-like transport again | highest (declarative runtime, A3 M3) |
| As-of safety | unchanged | unchanged | unchanged - same inputs (`poll`/`read_once`), same completed-bar filtering (`_completed`, `m5[:-1]`) | unchanged | must be re-established for the new runtime |
| Restart behaviour | runner state file | runner state file (local disk) | state in PostgreSQL with version CAS and lease; **survives loss of the host disk** | as A | as C |
| Decision-trace fidelity | Context L1 / Liquidity L2 via tailer | same, but from memory not a file | same L1/L2, plus lifecycle events atomically with state | same | native L3 possible |
| Rollback | trivial | trivial | export DB runner state back to the compact-state file shape and restart the legacy runner (monotonic version guard, `09`) | trivial | run both; retire twin |
| Research/live parity | unchanged | unchanged | unchanged (same functions) | unchanged | requires parity evidence |
| **File IPC reaches zero?** | **No** (runner -> adapter file hop is service-to-service) | **Conditionally**: no cross-process file read, but the runner's *state file is still required for restart* | **Yes** | **No** in spirit (FIFO/tmpfs are files); needs source edits otherwise | **Yes** (after cutover) |
| Acceptable as migration state? | **Yes** - it is the P2 tailer state | only as an intermediate | - | no | later |

Variant **C'** (edit the runner sources to write PostgreSQL directly) is permitted by enforcement (`P16`/`P17`) and has precedent, but it changes bytes that Stage 1 policy froze and that provenance documents pin (`LEGACY_STRATEGY_PROVENANCE.md`), it is irreversible in the sense that the original file hashes disappear from the running system, and it is unnecessary given the seams above. **Not recommended.**

## 5. As-of safety and restart, in more detail

* **As-of.** Both runners already restrict decisions to completed bars (Context `_completed`, Liquidity `m5[:-1]`) and hand the strategy exactly the arrays they read. A wrapper that calls `poll(...)`/`detect(...)` with the same inputs cannot change what the strategy sees. What a wrapper *can* change is timing (poll cadence) and clock sources: it must use the runner's own `now()`/`now_iso()` (they feed event times) and not substitute DB time inside runner code paths.
* **Restart.** Context recovers from its compact state plus gap-recovery logic (`_recovery_context`); Liquidity re-evaluates the last ~18 anchors each poll (`detect`, idempotent by setup id). A DB-backed sink must return byte-identical state on `load_state` (canonical JSON round-trip with `sort_keys`, `default=str` semantics preserved) or restart behaviour drifts. This is the single most important parity test.
* **Event-before-state.** Today Context writes the event line and then checkpoints; a crash between them is absorbed by replay identity. A single-transaction sink (event + state) makes the ordering atomic, which is *stronger* than today and cannot change decisions; but it must be documented as a behavioural improvement of the wrapper, not of the runner.

## 6. Identity of what the wrapper runs

Do not claim the wrapper *is* the frozen strategy. It **hosts** it. `09` defines the identity record and what is and is not inside the `Evaluation` hash (`P20`: `provenance` and `runtime_version` are hashed).

## 7. Summary

| Question | Answer |
|---|---|
| Does "frozen" require persistence/output code to stay byte-identical? | **Not by enforcement**; by Stage-1 *policy* yes. A wrapper needs neither exception. |
| Can adapters invoke frozen decision logic without editing it? | **Yes.** Seams exist (`P23`, `P24`) and one adapter already does it (`LiquidityLegacyRuntime`). |
| Is a permanent runner exception to `PRODUCTION_FILE_IPC=0` necessary? | **No**, provided the recommended progression (`09`) is completed; option A is acceptable only as the migration state. |
