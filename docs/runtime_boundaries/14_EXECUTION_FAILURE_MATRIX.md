# 14 - Broker execution failure matrix

Target state: design `04` (fence F), attempt machine `05`, reconciliation `06`. "DB" = PostgreSQL attempt row. "Bridge" = write ledger state (`04` 2.3). "Broker" = what may exist at MT5. "Safe retry?" = may a **new attempt** for the same intent be created *now* (never a resend of an ambiguous one). Generation `g` = generation of the acting owner; `g+` = a later owner.

Notation for who/what proves not-sent: **PA** / **PB** / **PC** = proof classes A/B/C of `06` 3.1.

## 1. Stage-by-stage (single owner)

| # | Failure point | Durable DB state | Bridge state | Possible broker state | Safe retry? | Reconciliation required? | Fence behaviour | Operator action |
|---|---|---|---|---|---|---|---|---|
| 1 | before claim (event received, tx1 not committed) | intent `PENDING`, no attempt | none | none | yes | no | n/a | none |
| 2 | after claim, before `SENDING` (crash / pre-send validation fails / freshness lost) | attempt `CLAIMED` | none | none | yes (sweeper -> `FENCED`/`CANCELLED`, then new attempt if fresh) | no | tx2 re-asserts generation | none |
| 3 | after `SENDING` committed, before the HTTP request left (process dies) | `SENDING` | no ledger entry | none | **not yet**: `UNKNOWN` at bridge is only proof **PB** once the fence is advanced beyond `g` | yes: `write-status` -> `UNKNOWN` + fence > `g` => `NOT_SENT` | new owner's advance closes the late-arrival path | none if PB holds; else page |
| 4 | request accepted by bridge, `QUEUED` | `SENDING` | `QUEUED` | none | only after `write-cancel` returns `CANCELLED` (PA) | yes | advance cancels (`CANCELLED_FENCED`) | none |
| 5 | queued, deadline (5 s) expires | `SENDING`; client sees `isError` "EA did not respond" (frozen behaviour) | `EXPIRED_BEFORE_DISPATCH` or `TIMED_OUT` (never dispatched) | none | after status shows never-dispatched (PA) | yes (status) | n/a | none |
| 6 | **delivered to EA** (`dispatch()` True) | `SENDING`/`SENT` | `DISPATCHED` | order **may** be sent | **no** | yes: ledger result or broker match | cannot cancel; advance lists it | none unless `UNCERTAIN` > horizon |
| 7 | EA received the line but the poll response never reached it (WebRequest failure) | `SENDING`/`SENT` -> `UNCERTAIN` | `DISPATCHED`, no result | none (indistinguishable from 6) | **no** | yes: broker match; EA polled since; then operator (PC) | as 6 | operator attestation if no evidence |
| 8 | broker accepted | `SENDING` -> `CONFIRMED` (tx3) | `COMPLETED` with retcode/order/deal | position/order exists | n/a (terminal) | no (already evidenced) | tx3 asserts generation; if lost, recovery by `g+` from ledger | none |
| 9 | broker rejected (definitive retcode) | `REJECTED` (tx3) | `COMPLETED` | none | policy: default no retry (matches today) | no | as 8 | none |
| 10 | broker returned an **ambiguous** retcode (timeout/connection class) | `UNCERTAIN` | `COMPLETED` (ambiguous payload) | unknown | **no** | yes: broker match | as 8 | page if unresolved |
| 11 | bridge result lost to platform (HTTP response lost; bridge `COMPLETED`) | `SENDING` -> `UNCERTAIN` | `COMPLETED` + payload | as payload | no, until resolved | yes: `write-status` returns payload -> `CONFIRMED`/`REJECTED` | n/a | none |
| 12 | platform result lost (tx3 fails, database unavailable) | `SENDING` (holder retries tx3) | `COMPLETED` | as payload | no | yes (holder replays; on death recovery via ledger) | holder cannot renew lease without DB: sends stop after TTL | page if DB down > horizon |
| 13 | EA executed, result POST lost (bridge down / partition) | `UNCERTAIN` | `DISPATCHED` (or `ORPHANED_DISPATCHED` after restart), no result | position may exist | no | yes: broker match (`06` 4) | n/a | operator if ambiguous |
| 14 | bridge waiter timeout after `DISPATCHED` (frozen anomaly) | `UNCERTAIN` (`BRIDGE_TIMEOUT`) | `TIMED_OUT` then possibly `LATE_RESPONSE` captured in ledger | may exist | no | yes: ledger late payload / broker match | n/a | none |
| 15 | bridge restart while `QUEUED` | `SENDING` | `ORPHANED_QUEUED` | none | after PA | yes (status) | fence reloaded or UNSET (writes refused until re-advance) | none |
| 16 | bridge restart while `DISPATCHED` | `UNCERTAIN` | `ORPHANED_DISPATCHED` | may exist | no | yes | as 15 | operator if ambiguous |

## 2. Ownership changes at each stage (A at `g`, B at `g+1`)

| # | Ownership changes during... | Durable DB state | Bridge state | Possible broker state | Safe retry? | Reconciliation | Fence behaviour | Operator action |
|---|---|---|---|---|---|---|---|---|
| 17 | `CLAIMED` (A paused after tx1) | `CLAIMED` (gen `g`) | none | none | B: yes (adopt -> `FENCED`, new attempt if fresh) | no | A's tx2 fails `assert_generation` | none |
| 18 | after tx2, before A's HTTP request (A paused; interleaving I of `03`) | `SENDING` (gen `g`) | none / rejected `FENCE_STALE` on arrival | none | B after PB | yes: `UNKNOWN` + fence `g+1` => `NOT_SENT` | **CLOSED**: A's late request refused | none |
| 19 | request `QUEUED` (interleaving II) | `SENDING` | `CANCELLED_FENCED` (in advance ack) | none | B after PA | no (ack carries it) | **CLOSED**: cancelled atomically | none |
| 20 | request `DISPATCHED` (interleaving III) | `SENDING`/`SENT` | `DISPATCHED` (in advance ack `dispatched_unresolved`) | may exist | **no** | yes (B: `UNCERTAIN`, account halted) | cannot cancel; accounted | none unless unresolved |
| 21 | between PG acquire and advance ack (G3 interval, IV) | `SENDING` (gen `g`) | accepted (bridge still at `g`), authorization <= 5 s | may exist | no | yes: ledger via status | bounded by TTL; A cannot mint anew | none |
| 22 | after `CONFIRMED` at broker, before tx3 | `SENDING` | `COMPLETED` | position exists | n/a | yes: `g+` records `CONFIRMED` + ownership from ledger | tx3 by `g+` allowed on recovery edge | none |
| 23 | B crashes after acquire, before advance | `SENDING` etc. unchanged | bridge at `g`; grant expires in <= 20 s -> refuses all | as above | no | yes when a new owner appears | fail closed by grant expiry | page: no holder |

## 3. Infrastructure and environment

| # | Condition | Durable DB state | Bridge state | Possible broker state | Safe retry? | Reconciliation | Fence behaviour | Operator action |
|---|---|---|---|---|---|---|---|---|
| 24 | PostgreSQL unavailable **before** claim/tx2 | unchanged | none | none | n/a | no | no claim, no authorization; executor stops acting | page |
| 25 | PostgreSQL unavailable **after** send | `SENDING` | as ledger | as ledger | no | on recovery | lease cannot renew; grants expire; no new sends | page |
| 26 | NATS unavailable | intents may be created, events accumulate in outbox | none | none | intents that age > 5 s become `EXPIRED` | no | not a fencing input; **no file fallback** | alert |
| 27 | partition platform <-> bridge | `SENDING` -> `UNCERTAIN` if a request was in flight | unchanged | as ledger | no | yes once reachable | grants expire at the bridge; new owner cannot advance => cannot send | page |
| 28 | duplicate event delivery | inbox hit / state != `PENDING` | duplicate `attempt_id` => ledger entry | unchanged | n/a | no | n/a | none |
| 29 | same intent seen under different generation | earlier attempt non-terminal | per-attempt key differs | may exist | **no** while any earlier attempt is `SENDING`/`SENT`/`UNCERTAIN` (I5) | yes | later owner adopts, cannot re-send | none |

## 4. Operations and modes

| # | Case | DB / bridge / broker | Safe retry? | Reconciliation | Fence behaviour | Operator action |
|---|---|---|---|---|---|---|
| 30 | **Emergency close** (operator) | attempt `REDUCE_ONLY` via break-glass; DB may be down (journal replayed later) | idempotent by ticket | goal-state (`06` 6) | break-glass key, exp <= 60 s, no generation | audited; verify position gone |
| 31 | **Trade Manager management command** (close/trail) | management attempt on the same machine | after PA/definitive `REJECTED` only | goal-state | same fence as executor; **REAL close/trail refused by the bridge today (`B4`)** | enable M7 first |
| 32 | **Manual action in the MT5 terminal** | none | n/a | broker-state reconciliation flags unowned/changed position | not fenceable | decide ownership adoption policy |
| 33 | **REAL_EXECUTION vs `REAL_SMOKE_TEST` vs `DEMO_EXECUTION`** | smoke and real share the lease resource; demo uses legacy `mt5_market_order` | per mode | per mode | smoke cannot run concurrently with the automated executor | run smoke only with executor stopped/handed over |
| 34 | Account context changes in the terminal between check and send | `SENDING` | as ledger | account mismatch detected on next read | not a fence issue | residual risk documented (`04` 9) |

## 5. Reading the matrix

* **Irreversible point:** rows 6-7. From `dispatch()` True the outcome is unknowable to the bridge and irrevocable; every later row assumes accounting, not prevention.
* **Rows the fence turns from "OPEN" to "CLOSED":** 4, 18, 19 (queued/late-arriving stale requests).
* **Rows the ledger turns from "operator guess" to "evidence":** 3, 5, 11, 14, 15.
* **Rows that remain UNCERTAIN by nature:** 6, 7, 10, 13, 16, 20, 21 - the design guarantees they are *enumerated, blocking and reconciled*, not prevented.
