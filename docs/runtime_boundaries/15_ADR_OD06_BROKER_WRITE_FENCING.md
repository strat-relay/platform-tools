# ADR-0003 (proposed) - Broker-side write fencing for execution ownership (OD-06)

| | |
|---|---|
| **Status** | **RECOMMENDED - not APPROVED.** Caleb / the architect must approve before any implementation. |
| Date | 2026-09-21 |
| Deciders | Caleb (owner/architect) |
| Inputs | A4 `89ba8b2` (OD-06), A1 `eda827c` (BrokerWriteGate), Codex V1.2 `39d6216`, V1.2.1 `88528e6`; source evidence `tools/verify_evidence.py` (37 checks) |
| Supersedes | nothing; refines A4 `docs/migration/08` (fencing) and A1 `03` (WriteAuthorization) |

## Context

V1.2.1 gives PostgreSQL monotonic ownership generations and rejects a stale *acquire* (`platform.acquire_ownership`, `STALE_FENCING_GENERATION`). That protects **database** state. It does not protect the broker: process A can pass its lease check, pause, lose ownership to B (generation 41), wake, and call the MT5 bridge. PostgreSQL cannot recall an order already sent.

Facts that constrain the decision (all source-verified):

1. The bridge has **no authentication** and no idempotency ledger; the idempotency key is validated and ignored, and the EA never receives it (`B1`, `B2`, `B3`).
2. The **only cancellation point** for a write is `RequestLifecycle.dispatch()`; after it, the EA executes with no cancel protocol (`B6`, EA `OrderSend`).
3. Between the sender's last check and `dispatch()` there is a network hop, admission, a queue wait of up to 5 s and, before that, an `order_check` round trip through the same serialised EA queue (`01`).
4. The consumer does **not** use the extracted `Mt5ExecutionClient` (`P1`); management writes use a separate raw path that the bridge currently refuses for REAL (`P6`, `B4`).
5. No generation is stamped on intents or sends; the foundation has no function that validates a generation on a write path (`P13`); the decision row is written after the send (`P2`).
6. The frozen order-send timeout behaviour must not be redesigned (`B5`, A1 L6).
7. The bridge must remain a narrow adapter.

## Decision (recommended)

Adopt **design F**: a **bridge-held, authenticated, expiring fence** enforced at enqueue *and inside `dispatch()`*, plus a **signed, request-bound write authorization**, with the platform remaining the sole owner of lease, generation, intent and attempt state.

1. Ownership stays in PostgreSQL. A new owner acquires the lease, obtains a signed **fence grant** from the platform's fence authority, and **advances** the bridge (`POST /fence`) *before its first write*. The advance atomically (under the queue lock) cancels queued lower-generation writes, records the new generation (persisted, monotone), and returns the lower-generation requests already dispatched.
2. Every write carries a **WriteAuthorization** (`resource`, `generation`, `attempt_id`, `tool`, `request_fingerprint`, `scope_class`, `exp <= 5 s`, `key_id`, signature). The bridge verifies it at enqueue and re-checks fence + expiry at `dispatch()`.
3. The bridge gains a **write ledger** keyed by `attempt_id` (one enqueue per key; states; late EA responses) and `write-status` / `write-cancel` endpoints. The platform's bridge idempotency key becomes per **attempt**.
4. The platform adopts a **durable attempt state machine** (`CLAIMED -> SENDING -> SENT -> CONFIRMED/REJECTED`, with `FAILED`, `FENCED`, `CANCELLED`, `NOT_SENT`, and first-class blocking `UNCERTAIN`), commits `SENDING` before the call, never resends from ambiguity, and halts an account while an attempt is `UNCERTAIN` (subject to owner sign-off).
5. Fail closed everywhere: unknown/expired/unauthenticated => no dispatch; unknown fence after a bridge restart => writes refused until re-advanced.
6. The EA and the frozen timeout behaviour are unchanged.

### What is guaranteed - and what is not

* **Guaranteed:** after `fence.advance(R, g+1)` is acknowledged, nothing authorised under generation <= g can be dispatched to the EA for R; queued work is cancelled; already-dispatched work is **enumerated** to the new owner.
* **Bounded:** between the PostgreSQL acquire commit and the advance acknowledgement, the old owner's already-minted authorization (<= 5 s) is the only stale authority; the send it may cause was recorded (`SENDING` committed before the call) and is reconciled.
* **Not guaranteed (impossible):** that no order executes after the old owner "lost" ownership in PostgreSQL's view. Orders already dispatched or committed inside the bounded window can execute; they are never unaccounted for.

## Alternatives considered

| Alt | Summary | Outcome |
|---|---|---|
| A. Bridge reads PostgreSQL | freshest check | **Fallback.** Turns the bridge into a DB client; halts all writes (incl. reduce-only) on any bridge-DB partition |
| B. Signed capability only | auth + TTL | Necessary, insufficient: stale token valid to TTL; does not cancel queued work (kept inside F) |
| C. Gateway owns lease, sole writer | serialisation | Does not close the race (paused gateway = stale owner); the bridge is already the natural single serialisation point (subsumed by F) |
| D. Pushed generation only | ratchet | Correct but needs authentication and expiry to be safe (kept inside F) |
| E1. EA-side fence | | Rejected: EA cannot reach PostgreSQL; recompile + re-attach; moves the same state to a riskier layer |
| E2. Deadline only | shorter max-age | Bounds, does not close |
| E3. `magic`/`comment` attribution | detective | Optional later; today the EA's reads do not return them (`B8`) |

## Consequences

**Positive:** closes the queued/late-arrival cases of the race; makes every bridge write attributable and replay-safe; gives the platform evidence to resolve `UNCERTAIN` without guessing; removes "anyone on :22348 can place a real order"; keeps the bridge generic.

**Costs / risks:**

* Bridge changes M1-M6 (and M7 to enable REAL reduce-only) in `mt5-native-bridge`; a shared `write-authorization.v1` protocol with test vectors.
* Platform work: fence authority, `assert_generation`, lease liveness, attempt table/transitions, halt policy, reconciler, moving both write paths onto the client (`12`).
* Key management (HMAC vs Ed25519, `13`) becomes an operational responsibility.
* The bridge fence is a **mirror** of PostgreSQL ownership: its correctness depends on the fence authority signing only after a PostgreSQL check; the G3 window is the price of not coupling the bridge to the database.
* Behaviour changes that need explicit approval: account halt on `UNCERTAIN` (today an uncertain outcome is recorded as a rejection and processing continues); replacing "retry management failures every loop" with reconcile-then-retry.

## Migration and rollout

Additive and off by default; per-listener `MT5_REQUIRE_WRITE_FENCE`. The platform refuses to arm REAL execution unless `/health.write_fence.required` is true. Enabled first on a **test bridge and stub broker** (duplicate-delivery, kill-during-takeover, restart, partition drills), then in shadow (authorizations minted and verified, not required), then required, as part of P5 (A4 `16`). Rollback: disable the requirement (returns to today's behaviour) only while quiesced and with the fence generation preserved; generations never decrease (A4 `08` 5).

## Preconditions before implementation is scheduled

1. Approval of this ADR (`OD-A5-5`) and of the bridge scope (`OD-A5-6`: which of M1-M8; HMAC vs Ed25519).
2. Decision on REAL reduce-only capability and break-glass (`OD-A5-7`).
3. Decision on the halt policy (`OD-A5-4`).
4. Verified retcode classification (`OD-A5-8`).
5. Decision on EA read enrichment (`OD-A5-10`).
6. The Codex P0/P1 substrate confirmed not to have moved execution authority (`16`).

## Decision record (to be completed by the owner)

`Decision: ____   Date: ____   Conditions: ____   Alternative chosen if not F: ____`
