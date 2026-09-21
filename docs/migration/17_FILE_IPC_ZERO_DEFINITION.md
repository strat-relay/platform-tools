# 17 - `PRODUCTION_FILE_IPC=0`: definition and audit

## 1. Definition

`PRODUCTION_FILE_IPC=0` means that, in the production configuration, **no production correctness or authority depends on the local filesystem**. Precisely:

| # | Requirement |
|---|---|
| 1 | **No file-based service-to-service transport**: no service reads a file that another service wrote in order to learn something happened, or to receive work. |
| 2 | **No JSON/JSONL polling between services** (including tailing another service's log, and the bridge lifecycle journal read across repositories). |
| 3 | **No file ownership authority**: position ownership, execution authority/resume generation, arming, activation, replay watermark, leases are PostgreSQL rows. PID files and `/tmp` stop files are not used as authority or as singleton locks. |
| 4 | **No file broker-state authority**: broker state is a PostgreSQL snapshot; `broker_state.json` does not exist or is a non-consumed export. |
| 5 | **No file execution-intent or management-intent transport.** |
| 6 | **No checkpoint file is required for correctness** (`09`, convergence test `11` §6). |
| 7 | **No fallback**: no code path reachable in production constructs a JSONL store when the mode is `DB_PRIMARY`. |
| 8 | The Control API and console read PostgreSQL, not runtime files. |

### Explicitly permitted to remain files

* **Configuration**: versioned policy/config files loaded at start (`CONFIG` artifacts) - provided run-time-mutable *authority* is in the database.
* **Research/export** output (ObservationStore research files, forward-paper summaries, daily reports, exports for analysis).
* **Debug** artifacts (smoke outputs, watcher outputs, logs) - never read by a service to make a decision.
* **Frozen legacy strategy runners' own persistence** (their state/event files) **as strategy evidence**, *if* the runners have not been re-platformed: a tailer/adapter reads them. **This is the one place where `PRODUCTION_FILE_IPC=0` needs an explicit qualification**: a file written by a frozen runner and consumed by an adapter *is* file transport between two processes. Two ways to reach literal zero (OD-05): (a) port the runners' persistence layer (permitted by the fingerprint scope but requires an owner decision and parity evidence), or (b) run the adapter **in-process** with the runner (the runner's file becomes private state of one process; nothing else reads it). Until one is chosen, the claim is `PRODUCTION_FILE_IPC=0` **for all platform services**, with the runner→adapter hop listed as a documented exception on the audit report.

## 2. The audit

Two halves; both must pass, and the report states the commit and configuration audited.

### 2.1 Static half (exists, read-only): `tools/audit_file_ipc.py`

```sh
python3 docs/migration/tools/audit_file_ipc.py inventory   # writes data/io_call_sites.csv + data/io_audit_baseline.json
python3 docs/migration/tools/audit_file_ipc.py audit       # exit 1 while IPC candidates exist
```

AST-parses the production modules (never imports or executes them) and lists every read/write/append/replace/unlink/exists call with module, line and function; classifies each as `IPC_CANDIDATE`, `ALLOWED_CONFIG_READ`, `ALLOWED_OFFLINE`, `DURABILITY_CALL` or `UNRESOLVED`. **Baseline at `64cb033`: 254 call sites in 72 files; 174 IPC candidates in 15 modules; 66 unresolved (calls on non-literal receivers such as `self.path`/parameters inside the runners, orchestrator and consumer, plus Control API readers, watch scripts and demo)** - so the real file surface is *at least* the candidate count and every unresolved site needs review. Target: 0 candidates outside an **allow-list** file kept in git with justification per entry (config load, research writer, frozen-runner exception). Unresolved sites must be reviewed and either classified or reduced to zero.

Limitations (stated so the audit is not over-trusted): it is heuristic (module-constant resolution one level deep, no data-flow); it cannot see paths built at run time or third-party writes. That is why the dynamic half exists.

### 2.2 Dynamic half (to be built): the "no filesystem authority" run

1. Start the **entire production stack** (orchestrator, execution, trade manager, broker-state publisher, projector-less, Control API) with `PRODUCTION_FILE_IPC=0`, `TRADING_PLATFORM_RUNTIME_DIR` pointing at an **empty, read-only** directory (or unset), and a **test broker stub**.
2. Run the end-to-end scenario: signal → route → sizing → intent → send → fill → management proposal → authorise → modify/close → result; plus restart of each service mid-scenario, duplicate delivery of every event, DB failover, JetStream outage.
3. Assert: every service healthy; scenario outcomes identical to the reference run; **zero** files created/modified under the runtime dir and under the repository working tree except the allow-listed research/debug/config paths (verified with an open-file trace: `fs_usage`/`dtruss` on macOS, `strace -f -e trace=openat` / `inotifywait` on Linux, or a FUSE/overlay that logs and denies writes).
4. Negative test: remove the database. Services must **stop acting** (fail closed) - and must not create JSONL files.
5. Positive control: run the legacy mode once and confirm the audit *detects* file IPC (proves the detector works).

### 2.3 Configuration assertion

With `PRODUCTION_FILE_IPC=0` the process refuses to start if it constructs any `JsonlStream`, `OrchestrationStore`, `ExecutionStore`, `SharedObservationPublisher`, or `BrokerStateStream` file implementation (or import of `platform_runtime.require_trading_platform_runtime` in a mode other than an explicitly named research mode). This is a unit-testable guard, complementing (not replacing) the two audits.

## 3. Audit report (what "done" looks like)

```
PRODUCTION_FILE_IPC audit  commit=<sha>  config=<hash>
static:   candidates=0 (allow-list entries=N, each justified), unresolved=0
dynamic:  scenario=PASS  files_written_outside_allowlist=0  db_down_fail_closed=PASS  positive_control=DETECTED
gates:    LEGACY_READ_RETIRE_READY=PASS  LEGACY_WRITE_RETIRE_READY=PASS
exceptions: [frozen runner -> adapter hop, if OD-05 not resolved]
```

## 4. Non-goals

This document does not perform the cutover, does not change production code, and does not alter any runtime.
