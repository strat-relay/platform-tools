# 18 - Codex V1.2 review checklist

For reviewing the PostgreSQL + JetStream **foundation** (CODEX-V1.2-POSTGRES-NATS-FOUNDATION) against this migration design, using the **clean V1.2 handoff commit only**. Each item states what must be true and where to look. Mark `PASS` / `FAIL` / `N-A (reason)`; any `FAIL` on items marked ★ blocks `DB_AUTHORITY_READY`.

| # | Check | Pass criterion | Look at |
|---|---|---|---|
| 1 ★ | **Schema vs authority requirements** | Every AUTHORITATIVE_STATE group in `04` has a table (or a documented reason it does not) with the PK/unique constraints named in `05`-`08` (`signal_id`, `execution_intent_id`, `idempotency_key`, `action_key`, `(position_id, source)`, `(resource, generation)`, `(account_context_id, snapshot_version)`) | migrations; `data/migration_matrix.csv` |
| 2 ★ | **Outbox atomicity** | Domain write and outbox insert share one transaction in the *same* connection/unit of work; no code path commits a state change without the outbox row (test exists that fails if omitted) | repository/unit-of-work code and tests |
| 3 ★ | **Outbox relay** | ordered per `ordering_key`; `Nats-Msg-Id = event_id`; `published_at` set only after publish-ack; lease per partition; bounded retention | relay code |
| 4 ★ | **Inbox transaction semantics** | inbox insert, effects, and any downstream outbox insert in one tx; ack **after** commit; duplicate → ack without effect | consumer base class/tests |
| 5 ★ | **Fencing** | `runtime_lease` with DB-time expiry and monotonic `generation`; authority writes validate the token in-tx; stale token rejected by the database; generation never decreases (incl. rollback) | schema + tests (`08` §7) |
| 6 ★ | **Runtime instance identity** | `runtime_instance_id` generated per process, recorded with modes/build/fingerprints, present in every envelope | bootstrap + envelope |
| 7 | **Event envelope** | fields of `03` §4 present; `payload_hash` verified; `schema_version` handled (unknown → park); traces/large evidence by reference | envelope schema/tests |
| 8 | **Subject/stream topology** | matches `03` §5 or deviations are justified: execution subjects partitioned by account; `OBSERVATION` isolated with bounds; DLQ defined; consumers durable/explicit-ack/bounded `max_deliver`/backoff | stream/consumer config |
| 9 ★ | **No DB-as-queue** | no consumer discovers work by polling domain tables; the outbox is read only by the relay | grep for polling loops on domain tables |
| 10 ★ | **No NATS-as-authority** | no decision is made from an event payload alone where a row exists; state can be rebuilt without JetStream | consumer handlers; rebuild test |
| 11 ★ | **No accidental file fallback** | in `DB_PRIMARY` no exception handler, default argument or env default constructs a JSONL/file store; missing runtime dir is not an error; guard test | config/bootstrap; `17` §2.3 |
| 12 | **Runtime modes** | state/event/projection axes and the validity table of `14` enforced at start-up; illegal combos refuse to start | config validation tests |
| 13 | **Matrix completeness** | every module reported by `audit_file_ipc.py` (`data/io_audit_baseline.json` → `ipc_candidates_by_module`) appears in `04`; every AUTHORITATIVE row has a reconciler spec (`11` §1) | diff the two |
| 14 ★ | **Execution idempotency** | unique `idempotency_key`; `PENDING→SENDING` CAS with fence and DB-time freshness; attempt committed **before** the bridge call; `UNCERTAIN` state; no automatic resend from `UNCERTAIN`; duplicate delivery test produces 0 broker writes (stub) | execution schema/handler/tests |
| 15 ★ | **Evaluation hash round-trip** | V1.1 `Evaluation`→canonical bytes→`evaluation_hash`/`trace_hash` identical after store/load/serialise; events carry only hashes+refs | tests around `core/strategies/evaluation` |
| 16 | **Dedicated infrastructure assumption** | own DB/roles/schema and own JetStream account/quotas stated; no shared-instance dependency; no shared-infra changes required by this repo | deployment docs/config |
| 17 | **Deterministic downstream ids** | handler-produced events derive `event_id` from `(causation_id, event_type)` | handler tests |
| 18 | **Freshness and time** | limits (`intent_max_age` 5 s, `signal_max_age` 120 s, broker snapshot 120 s) use DB time and are unchanged | code + config |
| 19 | **Broker state** | CAS on version; snapshot continuity from imported file version; no tick tables; refresh errors are metrics | schema + tests |
| 20 | **Projector** | single writer of legacy files in `DB_PRIMARY`; idempotent; canonical fidelity test; removable by config | projector + tests |
| 21 | **Reconciliation** | implements identity/hash/class rules of `11`; findings persisted; quiesced mode | reconciler |
| 22 | **Observability** | metrics/alerts of `15` exist or are tracked | metrics registry |
| 23 | **Security** | least-privilege roles (writer / relay / reader / migrator); credentials from environment; none in repo | migrations; secrets scan |
| 24 | **Retention/sizing** | outbox/inbox/stream retention configured and documented (OD-08) | config |
| 25 | **No production runtime change** | flags: no strategy/TM/execution policy edits; frozen fingerprints unchanged | diff vs baseline; fingerprint tests |
| 26 | **Portability** | no new cwd-relative runtime paths (existing ones listed in `04`: fanout, stream_consumer, lifecycle read) | grep |
| 27 | **Migration tooling scope** | tools that read legacy files are read-only; import tools are idempotent and hash-recorded (`import_manifest`) | tools |

## How to use with the handoff

1. Check out the V1.2 handoff commit into a **clean** worktree (do not inspect Codex's uncommitted work).
2. Run: `python3 docs/migration/tools/render_matrix.py --check` and `audit_file_ipc.py inventory`; diff results with this baseline.
3. Walk the table; record evidence (file:line or test name) per item.
4. Any `FAIL` on a ★ item: return to Codex with the item number and this document's section.

## Questions this checklist cannot answer (require the owner)

See open decisions in `00_README.md` (OD-01 … OD-08).
