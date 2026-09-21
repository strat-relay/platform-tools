# 16 - Review checklist for CODEX-V1.3-P0-P1-MIGRATION-SUBSTRATE

Use only the **clean V1.3 handoff commit** in a separate worktree; do not inspect Codex's uncommitted work. ★ = blocking. P0 = verify/baseline, P1 = substrate (schemas, roles, runtime instances/leases/heartbeats as *shadow rows*, streams/consumers created but inert, backup rehearsal, metrics) per A4 `docs/migration/16`.

## A. Scope and authority

| # | Check | Pass criterion | How |
|---|---|---|---|
| 1 ★ | **No authority cutover** | no domain is `DB_PRIMARY`; no code path makes PostgreSQL the source of truth for signals, sizing, execution, ownership, broker state, management or activation | grep for mode enums/config; run with all defaults and confirm every domain resolves to `LEGACY_FILE` (or `DB_SHADOW`) |
| 2 ★ | **Migration modes fail closed** | illegal combinations (A4 `14` 2) refuse to start; `DB_PRIMARY` selected while PostgreSQL is unreachable refuses to act; `DB_PRIMARY` cannot be selected at all for execution/ownership in P0/P1 | table-driven start-up tests |
| 3 ★ | **No implicit file fallback** | no `except` / default argument / env default constructs a JSONL or file store when a database mode is requested; a missing runtime directory is not an error in a database mode | grep + guard test (A4 `18` item 11) |
| 4 ★ | **OD-06 still explicitly blocks P5** | docs, config and status output state `READY_FOR_P5=false` / execution cutover not enabled; no `T_cut` tooling that can run; no dispatcher consuming `exec.*` events to act | read handoff; grep for cutover entry points |
| 5 ★ | **No production runtime behaviour change** | no edit to `live_execution_consumer.py`, `execution/demo_broker.py`, `trade_manager/`, `signal_orchestrator.py` behaviour, frozen strategy files, or the bridge; diff shows only additive substrate code | `git diff 88528e6..HEAD --stat`; fingerprints unchanged (`verify_evidence.py` P16, P17, P26 still pass) |
| 6 ★ | **Frozen strategy identity untouched** | Context decision fingerprint, Liquidity `source_hash`, runner file bytes unchanged | fingerprint tests; `git diff` on the runner files is empty |

## B. Tailer, projector, reconciliation

| # | Check | Pass criterion | How |
|---|---|---|---|
| 7 ★ | **Tailer does not lose partial lines** | a final line without a trailing newline is not consumed until complete; a torn line is retried, not skipped; truncation and rotation (`observation_stream` rotates at 50 MB) and file replacement via `os.replace` (state files: inode changes) are detected | tests with partial writes, rotate, replace, truncate |
| 8 ★ | **Tailer is read-only and non-intrusive** | opens legacy files read-only; never locks, renames, unlinks or writes in the runtime directory; offsets are derivable from the database (a checkpoint file is not needed for correctness) | strace/audit run; code review |
| 9 ★ | **Projector is idempotent** | replaying the same committed rows yields byte-identical legacy files; append-only files preserve `created_at`, `signal_timestamp`, `provenance` and order; the consumer's startup baseline and resume-cutoff filters (`create_intents` REAL branch) cannot release old signals | property tests; compare against legacy writer output |
| 10 ★ | **Reconciliation is not count-only** | identity + canonical hash + version/generation/terminal-state comparison, both directions, classes per A4 `11`; findings persisted | review reconciler; inject one-field mismatch and one missing row |
| 11 | **Known legacy defects are classified, not fixed** | the OD-01 output (`SKIPPED / MISSING_ACCOUNT_DATA / error 'tradeability_decisions'`) is expected and classed `KNOWN_LEGACY_DEFECT`; `tradeability_decisions` is **not** added to `OrchestrationStore.paths`; A4 findings (at-most-once fan-out checkpoint, scan-then-append, PID TOCTOU, undeclared streams) are recorded and unchanged | `verify_evidence.py` P9, P22 still pass; grep |
| 12 | **Unresolved file sites remain visible** | the static audit (A4 `audit_file_ipc.py`) is re-run; candidate and unresolved counts are reported against the A4 baseline, and any *reduction* is explained by a code change, not by classification/allow-list edits | run A4's tool from `89ba8b2` against the handoff commit |
| 13 | **Ownership and broker-state stay file-authoritative** | tailers create shadow rows only; nothing reads the DB shadow for `authorize()` decisions; the writer of both artifacts (the execution consumer) is untouched (A5 `11` 3) | grep readers of shadow tables |
| 14 | **Signals projector scope** | if P1 includes a signals projector it is off by default and has a reconciliation gate before any consumer could read its output | config review |

## C. Fencing groundwork (must not become enforcement)

| # | Check | Pass criterion | How |
|---|---|---|---|
| 15 ★ | **Leases/heartbeats are shadow** | `runtime_instances` / `ownership_leases` rows are written but not consulted to allow or deny any action by an existing service | grep call sites |
| 16 | **No accidental fence semantics** | if `assert_generation`, renewal, or `expires_at` handling is added, it is unused by execution; DB time (not process clocks) governs expiry | schema + tests |
| 17 | **No new coupling to the bridge lifecycle file** | no new reader of `runtime/execution_bridge/request_lifecycle.jsonl` (A5 `10`, A4 `EXE-15`) | grep |
| 18 | **No new cwd-relative runtime paths** | portability holes (fanout, stream_consumer, lifecycle read) are documented, not multiplied | grep for `Path("runtime`/`"runtime/` |
| 19 | **Execution schema not prematurely fixed** | if attempt/intent tables are touched, the 003-vs-008 overlap (`P15`) is resolved deliberately and the deltas of A5 `05` 7 are the reference; no code writes attempts from the legacy consumer | schema review |

## D. Infrastructure, security, definition

| # | Check | Pass criterion | How |
|---|---|---|---|
| 20 | **Dedicated infrastructure assumption** | own database/roles and own JetStream account/streams; no dependency on shared InfluenceLnk resources; no shared-infra change | deployment docs/config |
| 21 | **Streams/consumers are inert** | created consumers are `JETSTREAM_SHADOW`-style (no effect on any service); execution subjects are not consumed to act; no NATS-as-authority; no DB-as-queue | config review |
| 22 | **Secrets** | none in repo/images/logs; credentials from the environment | secrets scan |
| 23 ★ | **`PRODUCTION_FILE_IPC=0` definition has no permanent frozen-runner exception** | any updated definition/audit text shows the runner-to-adapter file hop as a **time-boxed S0 migration exception** ending at StrategyHost (A5 `09`), not as a permanent allow-list entry | read A4 `17` successor text |
| 24 | **Backup/restore** | PostgreSQL backup and restore rehearsed with measured RPO/RTO; the DR gap in `backup_runtime.sh` is recorded (A4) | runbook evidence |
| 25 | **Observability** | outbox/inbox/lag/reconciliation/lease metrics exist or are tracked (A4 `15`) | metrics registry |

## E. Mechanical re-verification

```sh
# from the handoff worktree
PYTHONDONTWRITEBYTECODE=1 python3 docs/runtime_boundaries/tools/verify_evidence.py
```

Expected: every check `PASS` **except** those that legitimately flip because P1 added schema (`P13`, `P14` may flip if `assert_generation`/state CHECKs are added; `P15` if the intent tables are reconciled). Any *other* flip (`P2`, `P5`, `P7`, `P8`, `P9`, `P11`, `P16`-`P18`, `P26`) means execution, orchestration, ownership or frozen code changed and must be explained. If the bridge repository moved, update `BRIDGE_COMMIT` in the tool deliberately.

## F. Findings that should appear in the V1.3 handoff notes

1. Which domains, if any, run `DB_SHADOW`, and which reconciliation gates were exercised.
2. The tailer's behaviour on partial lines, rotation and replacement, with test names.
3. Confirmation that OD-01, OD-02, OD-03 are recorded as open/decided per A5 `10`.
4. Confirmation that P5, the bridge change (A5 `12`) and OD-05 S1 (A5 `09`) are **not** started.
