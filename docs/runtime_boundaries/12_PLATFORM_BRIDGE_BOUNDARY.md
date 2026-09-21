# 12 - Platform / bridge responsibility boundary for OD-06

Derived from the mechanism in `04`, not from a template. Principle: the bridge implements the **smallest generic mechanism that can enforce a fence and account for what it dispatched**; everything that requires knowing *why* stays in the platform. The bridge remains "MT5 bridge tells MetaTrader what to do and reports what MT5 says."

## 1. Boundary table

| Capability | trading-platform | mt5-native-bridge | Why there |
|---|---|---|---|
| Lease, generation, ownership of `execution:real:<account>` | **owns** (PostgreSQL `platform.ownership_leases`, `assert_generation`) | none | truth about ownership is platform state |
| Intent, attempt state machine, retry/halt policy | **owns** | none | policy |
| Freshness / arming / account checks (`BrokerWriteGate`) | **owns** | none | policy; A1 seam |
| Fence authority: issue and sign grants and authorizations, rotate keys | **owns** | none | issuing requires PostgreSQL truth |
| Fence *state*: `resource -> (generation, grant expiry)`; monotone ratchet; persisted | none | **owns** (opaque string + integer) | must be enforced at `dispatch()`, under the queue's lock |
| Verify authorization: signature, `exp`, `tool`, fingerprint binding, scope class | mint | **verifies** (holds verification key(s) only) | the bridge is the last cancellable point |
| Cancel queued lower-generation writes on advance; enumerate dispatched ones | consumes the result | **performs** (atomic with the ratchet) | only the bridge holds the queue |
| Write ledger: one enqueue per `attempt_id`; state; EA payload incl. late responses | supplies `attempt_id`, reads status | **owns** (extends existing `request_lifecycle` journal) | the bridge is the only witness of dispatch and result |
| `write-status`, `write-cancel` endpoints | calls | **serves** | reconciliation evidence |
| Reconciliation, `UNCERTAIN` resolution, evidence packs, operator attestation | **owns** | none (serves evidence) | policy + audit |
| Broker primitive (`OrderSend`, close, trail) and its result | none | **owns** (EA, unchanged) | that is the bridge's job |
| Canonical request text / fingerprint | builds; shared vector tests | verifies equality (already: EA rebuilds text) | already shared (`protocol/v1`) |
| Mode/profile admission (which tools a listener exposes) | none | **owns as configuration** (`PROFILES`, A1 L7) | listener property, not policy |
| Account binding | includes `account_context_id` in the resource string; checks account context before send (already) | optionally restricts honoured resource prefixes by configuration | bridge does not learn accounts; it can only refuse resources outside its configured prefix |

## 2. The minimum bridge change set (what OD-06 asks of `mt5-native-bridge`)

| # | Item | Size | Compatibility |
|---|---|---|---|
| M1 | `POST /fence` (advance/renew): verify grant, atomic ratchet + cancel queued lower-generation + list dispatched, persist, reply with `bridge_epoch` | small | new endpoint; off unless enabled |
| M2 | Per-request `X-Write-Authorization` verification at enqueue **and** re-check inside `dispatch()` | small | required only when the fence is enabled for the listener |
| M3 | Write ledger keyed by `attempt_id` (index over the existing journal + new fields) with idempotent enqueue and `IDEMPOTENCY_CONFLICT` | medium | extends the journal schema (additive fields) |
| M4 | `GET /write-status`, `POST /write-cancel` | small | new endpoints |
| M5 | Persisted fence state (fsync before ack), `bridge_epoch`, UNSET on loss | small | new private file in the bridge's own runtime dir |
| M6 | `/health.write_fence {required, resources, bridge_epoch}` | trivial | additive |
| M7 | Enable REAL reduce-only tools (`mt5_close_position`, `mt5_trailing_stop`) on the execution listener under fence/break-glass scope, replacing the `EXPLICIT_DEMO_OR_SMOKE_MODE_REQUIRED` refusal for REAL | small but **behavioural** | needed for management execution (`01` 3); its own approval |
| M8 | Optional: EA read-side enrichment (`comment`, `magic`, order/position ids) | EA recompile + operator re-attach | optional hardening; **not** part of M1-M6 |

Explicitly **not** changed: the EA protocol and code (except optional M8), the frozen canonical-order-send timeout classification (`lifecycle.timeout()` for canonical sends; `isError` text) - A1 finding L6 - the priority scheduler, the research listener, the read cache/coalescing behaviour.

## 3. What the bridge must not learn

Strategy identity or rules, Trade Manager policy, subscription or commercial concepts, account meaning, intent semantics, retry/halt policy, reconciliation logic, lease TTL policy. It sees: opaque `resource` string, integer `generation`, `attempt_id`, `tool`, `scope_class`, `fingerprint`, expiries, key ids, and returns states and EA payloads.

## 4. Platform deliverables

| # | Item |
|---|---|
| Pl1 | `BrokerWriteGate` on the actual call path: replace `DemoExecutionAdapter` submit and the raw management `urlopen` with `Mt5ExecutionClient` carrying the signed authorization (`P1`, `P6`) |
| Pl2 | Fence authority (sign grants/authorizations; PostgreSQL check at issue time; key handling from environment/secret store) |
| Pl3 | `assert_generation`, lease renewal/liveness, attempt table + `transition_attempt`, partial unique index, account halt (`05` 7) |
| Pl4 | Reconciler and sweepers (`05` 6, `06`) |
| Pl5 | Advance-before-act in the executor start-up and on `bridge_epoch` change; arming refusal when `/health.write_fence.required == false` |
| Pl6 | Retcode classification (default-deny) and mapping of every bridge error text to an outcome class (no more free-text `str(exc)`) |
| Pl7 | Break-glass tooling and audit replay |

## 5. Shared contract

A `write-authorization.v1` protocol document with **test vectors** (canonical signing string, sample authorizations/grants using throw-away keys, expected accept/reject per fail-closed row of `07` 7) is the shared artifact, following A1's pattern for `canonical_request` (defined next to the verifier, vendored by the platform, contract-tested for byte equality). No real key material is ever placed in a repository.

## 6. Where each risk from `03` is owned

| Risk | Owner |
|---|---|
| G3 interval (acquire -> advance ack) | platform (advance immediately after acquire, TTL <= 5 s) and bridge (authorization expiry) |
| Dispatched-before-advance enumeration | bridge (returns the list) and platform (marks `UNCERTAIN`, reconciles) |
| Restart loses fence | bridge (persist, UNSET => fail closed) and platform (re-advance on `bridge_epoch` change) |
| Unauthenticated callers | bridge (verify) and platform (sign) |
