# StratRelay V2 Execution — Personal Execution Vertical Slice

Mission history: `CLAUDE-STRATRELAY-V2-EXECUTION-IMPLEMENTATION` (initial implementation,
commit `e17605e`) → independent Codex audit found blockers → this document, remediated by
`CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION`. **Implementation only — still not deployed.**
Written for the independent Codex re-audit this mission requires before any production
activation. Section 3 answers the original audit's 15 questions directly; section 3a answers
this remediation mission's own audit-relevant claims. Every answer points to the exact code/test
that proves it rather than asserting it.

## 0. Reconciliation with current production

The original implementation (`architecture/v2-execution`, commit `e17605e`) was built from an
older lineage (`564244b`) that did not yet include ENTRY_ONLY outcome canonicalization. This
remediation reconciles that source onto the actual current production commit,
`347e9d0804232a853ea009210c6bc970df801a44` (`codex/entry-outcome-canonicalization`), which
already includes: schema migration `015_entry_signal_outcomes.sql`
(`strategy.entry_signal_outcomes`), the canonical PostgreSQL outcome projection, the canonical
outcome/report API, P4.2 production lineage, the canonical Control API/router, and the canonical
P2 signal path. New worktree/branch: `architecture/v2-execution-audit-remediation`.

**The one required renumbering**: production already owns migration `015`. The V2 migration is
renumbered `postgres/migrations/016_execution_v2_foundation.sql`; every code/test/doc reference
to `015_execution_v2_foundation.sql` was updated to `016_*`. No table/schema/function name
collision existed between the two migrations (`015_entry_signal_outcomes.sql` only touches
`strategy.entry_signal_outcomes`; `016_execution_v2_foundation.sql` only touches
`execution_v2.*` plus the new, additive `platform.assert_generation()` function) - proven by
applying both, in order, against a real, empty PostgreSQL 16 instance
(`tests/test_execution_v2_real_postgres.py`, `tests/test_execution_v2_real_bridge.py`).
`postgres/foundation.py::DATABASE_SCHEMA_VERSION` was deliberately left at `"014"`, matching
production's own (pre-existing, unrelated) choice not to bump it for migration 015 either - it
gates the unrelated, already-deployed outbox relay/canonical signal publisher, and execution_v2
never reads it.

## 1. Scope

`EntrySignal → ExecutionEligibility → ExecutionIntent → risk validation → ownership/fence
acquisition → broker-held fence validation → MT5 execution request → ExecutionResult → canonical
persistence/event`, for Caleb's personal execution account only. No customer auto-trading, no
strategy-subscription execution, no portfolio optimization, no sophisticated position sizing, no
Trade Manager execution effects, no multi-account routing. See `execution_v2/__init__.py` for the
full, current statement of scope, isolation invariants, and the three distinct levels of proof
(section 2 below).

## 2. Three distinct levels of bridge-fence proof — do not conflate these

The original audit's central finding was documentation that implied level 1 was equivalent to
level 3. It is not. This remediation adds level 2 in full and corrects every place the prior
documentation blurred the distinction.

1. **Unit/simulator proof** (`execution_v2/bridge_fence_sim.py::BridgeFenceSimulator`,
   `tests/test_execution_v2_fence.py`): an in-process, in-memory, **TEST-ONLY** re-implementation
   of the OD-06 protocol - fast, dependency-free tests of the *protocol logic*. **Never imported
   by any production module** (`execution_v2/runtime/*.py`, `mt5_bridge_fence/*.py`) - proven by
   a static, AST-based transitive-import-closure check,
   `tests/test_execution_v2_isolation.py::test_no_production_module_imports_the_test_only_simulator`,
   immune to import-order/caching effects a `sys.modules`-based check would be vulnerable to. It
   cannot prove durability, restart behavior, or a real network boundary, and never claimed to
   after this remediation.

2. **Real bridge code, isolated proof** (`mt5_bridge_fence/` — `boundary.py` (`RealBridgeFenceBoundary`)
   + `store.py` (durable SQLite, WAL + `synchronous=FULL`) + `http_server.py` (stdlib
   `http.server`, real socket), exercised over a real HTTP transport by
   `execution_v2/runtime/bridge_client.py::HttpBridgeFenceClient`, proven end-to-end against real
   PostgreSQL + real NATS JetStream in `tests/test_execution_v2_real_bridge.py`). Only the FINAL
   MT5 order-send primitive (`broker_call`, injected into the bridge server at construction) is a
   fake/counting callable; every other check - signature, account binding, generation/epoch,
   expiry, request-fingerprint, idempotency, restart durability, reconciliation - is the real,
   production-intended code. **This is what `execution_v2/runtime/service.py` actually
   constructs and talks to** in production wiring; nothing about it is simulated.

3. **Live broker proof** — NOT performed by any code in this repository, and not performed by
   this mission. `mt5_bridge_fence/` is designed to be portable to the real `mt5-native-bridge`
   process (same validation/persistence logic, same wire shape over HTTP) but has never been
   deployed there, and this mission does not touch that repository, start port 22348, or send
   any live order. `BROKER_WRITES=0` for every test and every proof in this repository, always.

## 3. Answering the original audit's 15 questions

### Q1 — Can a stale worker's ordering ever reach the broker after it has lost authority?

No. Proven at three levels now:
- **Simulator level** (protocol proof): `tests/test_execution_v2_fence.py::StaleOwnerRejectedTests`.
- **Real bridge code level** (durable, over HTTP): `tests/test_mt5_bridge_fence.py::
  StaleOwnerRejectedTests` (including the mission's own worked example, generation 41→40→42) and
  `tests/test_execution_v2_real_bridge.py::test_stale_generation_zero_effect`.
- **Worker level, through production code and the real bridge**:
  `tests/test_execution_v2_worker.py::FenceRejectionThroughTheWorkerTests::
  test_stale_generation_is_rejected_and_produces_no_broker_effect` (simulator, protocol-level)
  and the real-bridge equivalent above (`live.calls == 0`).

### Q2 — Can duplicate delivery ever produce a duplicate broker effect?

No, and now proven durably (not just in-memory): `execution_intent_id` is deterministic from
`(entry_signal_id, account_id)`; `attempt_id` is deterministic 1:1 from `execution_intent_id` and
is the bridge's own durable idempotency-ledger key (`mt5_bridge_fence/store.py`'s
`idempotency_ledger` table, SQLite, fsync'd on every write). Proven by:
- PostgreSQL: `ON CONFLICT DO NOTHING` (unchanged from the original implementation).
- Real bridge, same process: `tests/test_mt5_bridge_fence.py::DurableIdempotencyTests::
  test_duplicate_submit_never_calls_the_broker_twice`.
- Real bridge, **new process, same on-disk file** (the strongest proof - see Q8):
  `test_idempotency_survives_a_real_process_restart_new_boundary_instance_same_db_file`.
- Full stack, real Postgres + real bridge over HTTP:
  `tests/test_execution_v2_real_bridge.py::test_duplicate_entry_signal_one_intent_one_effect`,
  `test_duplicate_jetstream_style_redelivery_one_effect`.

### Q3 — Can an ambiguous submission ever be blindly retried?

No. `broker_call()` returning `{"status": "AMBIGUOUS"}` is now durably recorded via a **two-phase
write** the real boundary performs (`mt5_bridge_fence/boundary.py::submit()`): `SUBMISSION_IN_PROGRESS`
is written and fsync'd BEFORE `broker_call()` is invoked, and only converted to `DISPATCHED`
(with the broker's response) AFTER it returns - so even the worst case (the broker accepted the
order but the process died before the second write) leaves a durable, unambiguous
`SUBMISSION_IN_PROGRESS` row, never silently treated as either success or as safe-to-retry.
`restart()` sweeps exactly that state to `UNCERTAIN_AFTER_RESTART`. Proven by
`tests/test_mt5_bridge_fence.py::CrashWindowTests::
test_crash_between_broker_accept_and_durable_success_write_is_never_lost_or_resubmitted` and,
full stack, `tests/test_execution_v2_real_bridge.py::test_response_loss_one_effect_reconciled_safely`.

### Q4 — Can execution ever happen while `EXECUTION_AUTHORITY_MODE` is disabled?

No - unchanged from the original implementation, and re-proven against the real bridge:
`tests/test_execution_v2_real_bridge.py::test_authority_disabled_zero_effect_through_the_real_bridge`.

### Q5 — Can the bridge ever execute without a currently valid fence?

No - now proven against the REAL, durable bridge, not only the simulator. Every `advance_fence`/
`submit` call independently re-verifies signature, generation, expiry, and account binding
against `mt5_bridge_fence/store.py`'s on-disk state before ever invoking `broker_call`. Proven by
the full `tests/test_mt5_bridge_fence.py` suite (16 tests) and
`tests/test_execution_v2_real_bridge.py`'s zero-effect matrix.

### Q6 — Can a fence minted for one account ever authorize execution on another account?

No. `tests/test_mt5_bridge_fence.py::WrongAccountRejectedTests` (real boundary) and
`tests/test_execution_v2_real_bridge.py::test_wrong_account_zero_effect` (over real HTTP,
exception type preserved end-to-end: `WrongAccount` propagates from the bridge server's JSON
error body back through `HttpBridgeFenceClient` as the same Python exception class).

### Q7 — Does fence expiry fail closed?

Yes, now proven against the real, durable boundary too:
`tests/test_mt5_bridge_fence.py::ExpiredFenceRejectedTests`,
`tests/test_execution_v2_real_bridge.py::test_expired_fence_zero_effect`.

### Q8 — Is idempotency preserved across a restart?

Yes, and this remediation makes the proof durable rather than merely process-local:
- **Worker restart** (a new `ExecutionWorker`, same PostgreSQL, same bridge):
  `tests/test_execution_v2_real_bridge.py::test_worker_restart_one_effect`.
- **Bridge restart** (a brand-new `RealBridgeFenceBoundary` + HTTP server instance, pointed at
  the SAME on-disk SQLite file - the strongest possible proof, since nothing in-memory survives):
  `tests/test_mt5_bridge_fence.py::DurableIdempotencyTests::
  test_idempotency_survives_a_real_process_restart_new_boundary_instance_same_db_file` and
  `tests/test_execution_v2_real_bridge.py::test_bridge_restart_one_effect` (the new bridge
  process's own call counter stays at 0 - the broker was never called a second time by the
  restarted process at all).
- A row that reached `DISPATCHED` (broker response durably recorded) before a restart is
  correctly left as `DISPATCHED`, not downgraded to uncertain - a real improvement over the
  in-memory simulator, which cannot distinguish a settled success from a genuinely-interrupted
  one and must treat every prior success as equally uncertain after a restart. See
  `mt5_bridge_fence/store.py::sweep_in_progress_to_uncertain`'s own docstring.

### Q9 — Are broker results truthful (never manufactured)?

Yes, with two real bugs found and fixed during the original mission (see §6) and reconfirmed
here: `HttpBridgeFenceClient.submit()`'s `broker_call` parameter is accepted for interface parity
but **intentionally never invoked** - the real bridge process owns the decision, not the
platform. `execution_v2/runtime/consumer.py`'s `_real_bridge_not_wired` sentinel remains as
defense in depth in case a bridge implementation is ever wired that DOES call it locally (proven
by `tests/test_execution_v2_runtime.py::
test_defense_in_depth_sentinel_fires_if_any_bridge_ever_invokes_broker_call_locally`), while the
actual production path (`HttpBridgeFenceClient`) is proven to never touch it at all
(`test_enabled_mode_with_the_real_http_bridge_client_never_touches_the_local_sentinel`).

### Q10 — Did P4.2, strategy/decision logic, or ENTRY_ONLY outcome semantics change?

No. Confirmed against the RECONCILED production lineage specifically (not just structurally):
`execution_v2`'s only cross-schema reference remains the one read-only FK to
`strategy.entry_signals`; nothing in this mission's diff touches `strategy.entry_signal_outcomes`,
any outcome projection/report code, `trade_management.*`, `context_structure_retrace_forward.py`
(`_process_bar()` and the other four frozen decision functions), or the canonical Control
API/router. See §6's regression wall for the exact baseline-vs-remediated test signature
comparison against commit `347e9d0`.

### Q11 — Is any legacy filesystem IPC authoritative for this slice?

No - unchanged; `mt5_bridge_fence/` also has its own isolation tests now
(`tests/test_execution_v2_isolation.py::BridgeFenceIsolationTests`) confirming it too never
imports the legacy `execution`/`trade_management`/`control_api` packages.

### Q12 — Is migration 016 forward/rollback safe?

Yes - see §0 above for the renumbering and collision analysis, and §6 for the real-PostgreSQL
proof that production migrations through 015 apply cleanly, then 016 applies cleanly on top, with
both the outcome schema and the execution_v2 schema intact afterward.

### Q13 — Does the deployment default to non-executing?

Yes, now in FOUR independent, redundant ways (one new): (a) `replicas: 0`; (b)
`EXECUTION_AUTHORITY_MODE` absent → defaults to `DISABLED`; (c) the shipped risk policy has
`enabled: false`; (d) **new** — `V2_BRIDGE_FENCE_URL` has no default and `workload.yaml` ships a
deliberately-invalid placeholder, so even an operator who mistakenly enabled execution and
somehow bypassed the risk gate would still have a runtime that refuses to start at all, let alone
place an order, without an explicitly-provisioned real bridge endpoint.

### Q14 — Are any secrets committed or logged?

No - unchanged, plus: `HttpBridgeFenceClient` logs nothing beyond its configured `base_url`
(never a key or signature); `mt5_bridge_fence/http_server.py`'s handler explicitly silences
stdlib per-request access logging (`log_message` override, matching `execution_v2/runtime/
health.py`'s existing convention) so no request/response body (which could contain a signed
authorization) is ever logged.

### Q15 — Is this ready for controlled production deployment?

**Not yet, by design** — see §7 "Before activation."

## 3a. This remediation mission's own audit-relevant findings

### Real bugs found and fixed (not merely test bugs)

Carried forward from the original mission:
1. `ExecutionWorker.process_signal`'s `broker_call=None` default silently manufactured a FILLED
   result. Fixed by making the parameter required.
2. `ExecutionWorker.process_signal` did not catch bridge-raised `WrongAccount`/`InvalidSignature`/
   `RequestFingerprintMismatch` at the submission step. Fixed by wrapping the `submit()` call.

Found during THIS remediation:
3. **After moving the bridge-fence exception/result types into shared modules
   (`bridge_fence_errors.py`, `bridge_fence_types.py`) so the real boundary would not need to
   import the test-only simulator, `execution_v2/worker.py` and `execution_v2/reconcile.py` were
   left importing those same names FROM `bridge_fence_sim.py`** - a leftover that would have
   defeated the entire remediation (production code transitively importing the test-only
   simulator module, exactly the original audit finding). Caught by the new static isolation
   test itself failing (`test_no_production_module_imports_the_test_only_simulator`), not by
   manual inspection. Fixed by importing from the shared modules and typing `worker.bridge`/
   `reconcile_attempt`'s `bridge` parameter against a new structural `BridgeFence` Protocol
   (`bridge_fence_types.py`) instead of the concrete simulator class.
4. **`mt5_bridge_fence/http_server.py` initially used `ThreadingHTTPServer`**, but the SQLite
   connection `FenceStore` holds is thread-affine; a per-request thread crashed on the very first
   real HTTP request in manual testing (`sqlite3.ProgrammingError`). Fixed by switching to a
   single-threaded `HTTPServer` - a deliberate choice, not a workaround: fence-generation and
   idempotency-ledger state is exactly the state a race between two concurrent requests must
   never be allowed to corrupt, and this single-personal-account, low-volume slice has no
   throughput requirement that would justify accepting that risk instead.
5. **The initial "any restart marks every DISPATCHED entry as uncertain" design (ported directly
   from the simulator's coarser in-memory model) was more conservative than the real durable
   store needs to be.** Corrected to a two-phase `SUBMISSION_IN_PROGRESS` → `DISPATCHED` write so
   restart only casts doubt on genuinely unresolved attempts, not settled ones - see Q8.

### Fence key / authentication design (mission section 4)

- **Signing algorithm**: HMAC-SHA256 (`execution_v2/fence.py::_sign`/`verify`), shared-secret
  (V1 per the OD-06 ADR's own stated hardening path; asymmetric signatures are documented future
  work, not required now).
- **Canonical payload**: `core.strategies.evaluation.canonical_bytes` (sorted-key, compact JSON) -
  the same deterministic serialization used throughout this codebase for hashing/signing, applied
  to `FenceGrant.signed_payload()` / `WriteAuthorization.signed_payload()` (every field except
  `sig` itself).
- **Key identifier/version**: `key_id` is carried in both signed structures and in the signature
  verification lookup (`keys: dict[key_id, secret_bytes]`); `V2_FENCE_KEY_ID` (default
  `v2-fence-key-1`) selects which key `FenceAuthority` signs new grants/authorizations with.
- **Expiry representation**: ISO-8601 timestamps with millisecond precision
  (`FenceGrant.not_after`, `WriteAuthorization.exp`), compared against `datetime.now(timezone.utc)`
  read from an injectable `clock` callable (real wall-clock in production; controllable in tests).
- **Clock-skew policy**: none implemented - expiry comparison is a strict `now > expiry` check
  with no grace window. Given the short TTLs involved (`DEFAULT_GRANT_TTL_MS = 20_000`,
  `DEFAULT_AUTHORIZATION_TTL_S = 5.0`) and that both the platform and the bridge would run in the
  same infrastructure with NTP-synchronized clocks, this is a deliberate, documented simplification,
  not an oversight - flagged in `docs/engineering/OPTIMIZATION_REGISTER.md` for revisit once the
  real bridge's actual clock-sync guarantees are known.
- **Rotation behavior**: `FenceAuthority` holds a `keys: dict[key_id, secret_bytes]` map plus one
  `active_key_id` used for new signatures; older keys may remain present so in-flight
  grants/authorizations signed under them still verify during a rotation window. The verifying
  side (`mt5_bridge_fence`/the simulator) looks up by whatever `key_id` the payload itself
  carries, never assumes "the" key. See `deploy/execution_v2/README.md`'s Rollback section for
  the operational rotation/revocation procedure.
- **Failure behavior**: fails closed at every layer - `FenceAuthority.__init__`/`.from_env()`
  refuse to construct without a real, ≥32-byte key (`FenceAuthorityError`); the bridge boundary
  raises `InvalidSignature` for an unknown `key_id` or a signature mismatch, never falling through
  to "treat as valid." No log statement prints key material (see Q14).

### Monotonic generation at the bridge (mission section 5)

`mt5_bridge_fence/store.py`'s `fence_state` table persists `(resource, generation, holder,
grant_expires_at, advanced_at)` on disk, keyed by resource, surviving process restart (proven by
`tests/test_mt5_bridge_fence.py::DurableIdempotencyTests` restart tests and the bridge-restart
test in `test_execution_v2_real_bridge.py`). `advance_fence()` rejects any grant whose generation
is strictly less than the currently-stored one (`StaleGeneration`) and, on a genuine advance,
durably cancels any still-pending ledger entry for that SAME resource
(`cancel_pending_for_resource`, scoped by resource - a correctness improvement over the
simulator's documented global-sweep simplification). The mission's own worked example (generation
41 accepted; 40 rejected; 41 and 42 both still valid in their own right) is reproduced verbatim as
a test: `tests/test_mt5_bridge_fence.py::StaleOwnerRejectedTests::
test_example_generation_41_then_40_rejected_then_42_valid`.

## 4. Scope reductions (explicit, deliberate, documented for a future mission)

- **One attempt per intent.** Re-attempting after a proven-safe terminal state is deferred.
- **One intent per entry signal, globally** (not per-account) - correct for a single
  personal-execution account.
- **HMAC-SHA256 (V1 shared-secret) signing** - asymmetric signatures are documented future work.
- **No clock-skew grace window** between platform and bridge (see §3a).
- **NATS-first execution optimization is not performed** - the outbox/relay path is reused
  unmodified.
- **The real bridge's HTTP transport is not authenticated at the network layer beyond the signed
  payload itself** (no mTLS/API key on the HTTP connection) - the payload-level HMAC signature IS
  the authentication for what matters (whether to act on a request), but transport-layer hardening
  (e.g. binding the bridge to a private network segment, or adding mTLS once it is actually
  deployed alongside the real `mt5-native-bridge` process) is separate, later, necessary work.

## 5. Real-infrastructure proof: exact commands used during this mission

```bash
docker network create v2audit-net
docker run -d --name v2audit-pg --network v2audit-net -e POSTGRES_USER=v2audit \
  -e POSTGRES_PASSWORD=v2audit -e POSTGRES_DB=v2audit -p 15644:5432 postgres:16-alpine
docker run -d --name v2audit-nats --network v2audit-net -p 14345:4222 nats:2.10-alpine -js

TRADING_POSTGRES_DSN="postgresql://v2audit:v2audit@localhost:15644/v2audit" python3 -m postgres.migrate
TRADING_POSTGRES_DSN="postgresql://v2audit:v2audit@localhost:15644/v2audit" \
  python3 -m unittest tests.test_execution_v2_real_postgres -v
V2_TEST_NATS_URL="nats://localhost:14345" python3 -m unittest tests.test_execution_v2_real_nats -v
TRADING_POSTGRES_DSN="postgresql://v2audit:v2audit@localhost:15644/v2audit" \
  V2_TEST_NATS_URL="nats://localhost:14345" \
  python3 -m unittest tests.test_execution_v2_real_bridge -v

docker rm -f v2audit-pg v2audit-nats && docker network rm v2audit-net
```

## 6. Test inventory

| File | What it proves | Needs |
|---|---|---|
| `tests/test_execution_v2_fence.py` | OD-06 protocol proof against the test-only simulator (14 tests) | nothing (pure/fake) |
| `tests/test_mt5_bridge_fence.py` | Same OD-06 proof list against the REAL durable boundary, plus restart/crash-window durability (16 tests) | nothing (real SQLite, tmp file) |
| `tests/test_execution_v2_intent.py` | Eligibility + idempotent intent creation | fakes.py |
| `tests/test_execution_v2_worker.py` | Full lifecycle, duplicate/idempotency/authority scenarios (simulator-level) | fakes.py |
| `tests/test_execution_v2_reconcile.py` | Reconciliation findings, never rewriting the original result | fakes.py |
| `tests/test_execution_v2_runtime.py` | Fail-closed `RuntimeConfig` (incl. `V2_BRIDGE_FENCE_URL`), consumer wiring against both the sentinel and a real HTTP bridge | fakes.py, real HTTP loopback |
| `tests/test_execution_v2_isolation.py` | No legacy imports, no simulator import from production (static closure), no `signals.jsonl`, no port 22348, migration DDL properties, `mt5_bridge_fence` isolation | static/AST |
| `tests/test_control_api_execution_v2_source.py` | Prepared canonical `/executions` read path | local fake cursor |
| `tests/test_execution_v2_real_postgres.py` | Production code against real PostgreSQL 16 (triggers, FKs, `assert_generation`) | Docker Postgres |
| `tests/test_execution_v2_real_nats.py` | Existing EXECUTION stream/subjects + unmodified `OutboxRelay` against real JetStream | Docker NATS |
| `tests/test_execution_v2_real_bridge.py` | **The capstone proof**: real Postgres + real NATS + real bridge code over HTTP + fake final MT5 primitive only, full duplicate/restart/rejection matrix (13 tests) | Docker Postgres + Docker NATS |

## 7. Before activation

- The real bridge-side fence service (`mt5_bridge_fence/`, designed to be portable) has never
  been deployed to the actual `mt5-native-bridge` process/repository. That integration work,
  and provisioning `V2_FENCE_SIGNING_KEY` to it out-of-band, is required first.
- An operator-approved, reviewed `v2_execution_risk_policy.json` (real `max_volume`,
  `allowed_accounts`, `allowed_symbols`) must replace the shipped blocked-by-default config.
- `V2_EXECUTION_ACCOUNT_ID` and `V2_BRIDGE_FENCE_URL` in `workload.yaml` must be replaced from
  their deliberately-invalid placeholders.
- A real `execution-v2-fence-signing-key` Secret must be provisioned, and the resource-quota
  patch in `deploy/execution_v2/resource-quota-patch.README.md` computed from the then-current
  live cluster state (no live-cluster read has been performed by any mission to date).
- The deployment-plan `NetworkPolicy` contains only a narrow egress rule to the bridge host
  (`192.168.1.166/32`, TCP 22348); it has not been applied. Verify the actual bridge address and
  policy enforcement before any separately authorized activation.
- `EXECUTION_AUTHORITY_MODE=ENABLED` must be set via its own separate, explicitly-authorized
  change - never bundled into any implementation mission's artifacts.
