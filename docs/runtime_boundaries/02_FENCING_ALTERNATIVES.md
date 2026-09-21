# 02 - Fencing alternatives

The race to close (from the task): owner A checks its lease, pauses, owner B acquires generation 41, A wakes and calls the bridge. PostgreSQL can reject A's later *database* write; it cannot recall a broker operation. The fence therefore has to be enforced **at the last point where a request is still cancellable**: `RequestLifecycle.dispatch()` (`01` section 6). Anything earlier is a check-then-act window; anything later is too late.

Threat model used throughout (stated so no design is judged against a different one): **trusted but slow.** Platform processes are correct but may pause (SIGSTOP, VM freeze, GC, swap, network stall) for arbitrary time between any two instructions, and may be partitioned. They are not adversarial. Security against other local callers is treated separately (`13`).

## 1. Candidates

| Id | Design | Where the decision is made |
|---|---|---|
| **A** | Bridge validates the request's fence generation against PostgreSQL | bridge, at enqueue and at dispatch, by a live DB read |
| **B** | Platform issues a short-lived signed execution capability (resource, generation, intent, expiry, operation); bridge verifies | bridge, from the token alone |
| **C** | An execution gateway owns the lease and is the sole bridge writer | platform component in front of the bridge |
| **D** | Bridge keeps the current accepted generation per resource, **pushed** by the platform; every write carries its generation | bridge, from a locally held integer |
| **E1** | Fence check inside the EA (MQL5) | EA |
| **E2** | Deadline only: make request lifetime shorter than lease time | bridge (existing `X-Bridge-Max-Age-Ms`) |
| **E3** | Carry the generation in the broker-visible request (`magic` / `comment`) for after-the-fact attribution | none (detective) |
| **F** *(recommended, see `04`)* | **D** as the enforcement mechanism (advance-before-act, checked at enqueue **and** dispatch, atomic with cancellation of queued lower-generation work) **plus B-lite** (short-lived signed authorization bound to the exact request and to the fence grant) | bridge dispatch gate; platform owns all state |

## 2. Assessment against the actual repository

| Criterion | A | B alone | C | D alone | E1 | E2 | **F = D + B-lite** |
|---|---|---|---|---|---|---|---|
| Closes "owner pauses between check and bridge call" | Nearly: residual = one DB round trip between the bridge's read and its dispatch (ms) | **No**: stale token stays valid until TTL | **No**: a paused gateway is the same race one hop earlier | **Yes once advance is acked**; before the ack, bounded by the request's TTL | Would (EA state) | **No**: a stale sender mints a fresh 5 s deadline when it wakes | **Yes once advance is acked**; before the ack, bounded by authorization TTL (<= 5 s) |
| Cancels work already queued before lease loss | Only if bridge re-checks at dispatch | No | No | **Yes** (advance cancels `QUEUED` lower-generation requests under `lifecycle.lock`) | n/a | Expiry only | **Yes** |
| Enumerates what already reached the EA | No | No | No | **Yes** (advance reply lists `DISPATCHED` lower-generation requests) | No | No | **Yes** |
| Bridge gains a runtime dependency on the platform DB | **Yes** (driver, credentials, network path, schema knowledge) | No | No | No | No | No | No |
| Availability coupling | bridge writes stop whenever DB is unreachable from the bridge host | none | gateway outage stops writes | none; grant TTL fails closed without DB | none | none | none |
| Bridge learns platform concepts | lease table, resource naming, SQL | key ids only | none | opaque `(resource, integer)` | none | none | opaque `(resource, integer, expiry, tag)` + verification key |
| Change to the EA | none | none | none | none | **recompile, re-attach, no PG reachability from MQL5** | none | none |
| Fail-closed when platform unreachable | yes (no DB, no dispatch) | token expires | yes | yes (grant expires; unknown state rejects) | n/a | partly | yes |
| Authenticates callers (today: nobody) | no | **yes** | no | no (advance itself must be authenticated) | no | no | **yes** |
| Replay / reuse of an authorisation | n/a | TTL + binding | n/a | n/a | n/a | n/a | TTL + binding to `attempt_id` + ledger uniqueness |
| Works with today's synchronous HTTP call | yes | yes | yes | yes | n/a | yes | yes |
| Complexity added to the bridge | high (DB client in a stdlib-only server) | low | none | medium (small state + one endpoint) | high | none | medium |

## 3. Why the alternatives fail or are subsumed

**A (bridge reads PostgreSQL).** It is the *freshest* check, and its only residual is one DB round trip. It is rejected as the primary design because it turns a deliberately narrow broker adapter into a database client: credentials on the bridge host, a network path bridge-to-PostgreSQL, a synchronous DB call in the `/poll` path (which holds a long-poll socket to the EA), and a partition between bridge and PostgreSQL that halts all writes including risk-reducing ones. It is kept as the **documented fallback** if the owner judges the residual of F unacceptable (`15` alternatives).

**B alone (signed capability).** A capability proves *who asked* and bounds *how long* it is usable; it does not know that the world changed after minting. With TTL <= 5 s the stale window is bounded, not closed. It is retained as the authentication, binding and replay layer of F.

**C (gateway owns the lease, sole writer).** In this repository the execution consumer already *is* the sole intended broker writer (`sole_write_owner` is a Control API expectation, `control_api/app.py:359`). Making it explicit does not close the race: a paused gateway is the stale owner. C is only useful when the gateway's own connection to the bridge is fenced - which is D. Note the structural fact that the **bridge is already the single serialisation point for broker writes** (one EA queue, one `lifecycle.lock`): it is the natural gateway, and D uses it as such.

**D alone (pushed generation).** Correct and small. Two additions make it safe in practice: the advance itself must be authenticated (otherwise any local caller can move the fence), and grants must expire (otherwise a bridge that loses its persisted state after a restart could be "resurrected" to an old generation by a stale process presenting an old grant). Those additions are the B-lite parts of F.

**E1 (EA-side).** The EA cannot reach PostgreSQL; it would need a pushed generation as well, plus persistence in MQL5, a recompile and an operator re-attach. It moves the same state one layer down at much higher risk. Rejected. (EA changes are separately considered for *read-side enrichment* in `06`.)

**E2 (deadline only).** The 5 s max-age already exists and is valuable, but a stale sender that wakes simply creates a new request with a new deadline. Rejected as a fence; kept as a bound.

**E3 (`magic`/`comment` attribution).** Detective only, and today the EA's read tools do **not** return `comment` or `magic` (`B8`), so it cannot even be used for reconciliation without an EA change. Kept as an optional later hardening.

## 4. Decision drivers, in order

1. The fence must be enforced at `dispatch()` (last cancellable point) under the same lock as the state it protects.
2. The bridge must remain a narrow adapter: it may hold an opaque `(resource, integer generation, expiry)` and a verification key; it must not hold platform state, credentials to platform stores, or policy.
3. Failure must be closed: unknown, expired or ambiguous => no dispatch.
4. Every request that *might* have reached the broker must be accountable (durable platform record before the call, bridge ledger entry, enumerable on takeover).
5. The EA and the frozen order-send timeout behaviour stay unchanged.

D + B-lite (=F) is the only candidate that satisfies all five. See `04` for the mechanism and `03` for the race-by-race analysis.
