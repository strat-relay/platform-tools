# Trade Manager code audit — 2026-10-19

Base: `bcc4964ae7e80570ac804381b33e8654e651ce56`.

## Scope and execution path

Inspected the versioned observation/decision runtime, breakeven/trailing and structure
evaluators, exit policy, management consumer, and personal execution boundary. The legacy
`trade_manager` package remains separate; its tests were included in validation.

`trade_management` records observations and decisions; the outbox sends
`trade.decision.made.v1` to execution-v2. Personal execution requires LIVE mode,
platform-linked fill/ticket, execution authority, freshness, broker position checks,
stop protection, deduplication and a fenced REDUCE_ONLY authorization.

The publication gate is a customer-distribution boundary. WITHHELD/SHADOW_ONLY there
does **not** prevent personal execution of an actionable decision in global LIVE mode.
Do not interpret publication status as proof that no broker action can occur.

## Confirmed defects fixed

| Defect | Previous behavior | Fix |
|---|---|---|
| Future decision timestamp | Negative age passed the 120-second age check | Reject future decisions |
| Position type validation | Every type except zero became SHORT | Accept only explicit MT5 buy/sell types |
| Ticket identity check | Ticket and direction checked, symbol unchecked | Require the broker symbol to match the linked fill |
| Invalid price levels | NaN/infinity could pass comparisons or fail later while fingerprinting; malformed values raised | Reject malformed/non-finite requested levels and invalid broker levels before authorization |
| Mode changed during ownership acquisition | Only execution authority was rechecked | Recheck LIVE mode after ownership acquisition, before submission |

Nine standalone regression tests cover rejected writes, valid stop moves, preserved
targets, freshness boundaries and a mode change during ownership acquisition.

## Validation

`python -m pytest -q tests/test_trade_management* tests/test_trade_manager* tests/test_tm_* tests/test_management_execution* test_trade_manager*.py`

Result: **249 passed, 23 skipped, 20 subtests passed**. Integration tests requiring
PostgreSQL/external services were skipped. `git diff --check` passed.

No production service, database, broker position or operating mode was changed.

## Remaining audit work requiring runtime evidence or separate changes

- Verify deployed image/commit, global TM mode, each open trade's bound version,
  observation freshness, consumer delivery health and management-intent outcomes.
  Repository inspection cannot establish whether the running manager is healthy.
- Replay any reported missed breakeven/trailing action with observation, decision,
  intent and bridge evidence. No problematic trade or runtime logs were supplied.
- Profit exit revaluation currently uses the decision's observed quote, rather than a
  newly fetched broker quote. Decision freshness alone permits up to 120 seconds of
  market movement. A separate quote-age/revaluation policy needs regression coverage.
- Structure decisions read completed bars from the current cache. The as-of filter
  removes future bars, but replay is not guaranteed once historical bars age out.
- Effective stop/target derivation uses theoretical prior decisions, not broker
  acknowledgments. Investigate missed/rejected actions before treating this state as
  the protection actually installed at the broker.
- Durable action-key deduplication can prevent an identical rejected/fenced request
  from being attempted by a later decision. Recovery needs an explicit distinction
  between definite pre-dispatch failure and ambiguous broker effect.

These items are not deployment validation and are not resolved by the boundary fixes.


## Theoretical time-exit correction

The scheduler now evaluates strategy-theoretical time exits before quote fetching and
terminal lifecycle reconciliation. Clock observations have no market snapshot; migration
068 permits that shape explicitly, without inventing prices. Existing open broker-anchored
deadlines are cleared and recomputed from canonical strategy time/policy on the next tick.

For SIGNAL_TIMESTAMP activation, the deadline follows the canonical resolver:
`signal.decision_time + captured time_exit_minutes`, or the outcome contract's
`max_hold_minutes` when no explicit TM hold was captured. A delayed broker fill does not
move this clock. ENTRY_PRICE_TOUCH signals require canonical TIME_EXIT evidence, because
an elapsed clock alone cannot prove a pending entry filled. Pending activation is not
inferred from broker execution. Strategies without a hold policy do not gain one.

A due deadline takes priority over the bound price evaluator, including TM-NONE. It
produces an EXIT/TIME_EXIT decision through the existing durable decision/outbox path.
An already-known STOPPED/TARGET_HIT or other non-time terminal outcome suppresses the
clock event; a canonical TIME_EXIT must not close the managed trade before that decision
is recorded. Stale quotes and missing stop geometry cannot block the clock path.

For a published entry, the same transaction inserts one deterministic
`signal.management.published.v1` event per managed trade/deadline. It includes the entry
signal id, management decision, theoretical outcome classification and TIME_EXIT reason.
The existing outbox relay reconciles the subject into TRADING_CORE and publishes it.
Retries/restarts reuse the observation and notification ids. This repository contains no
subscriber notification sender: delivery to a subscriber's channel must consume this new
subject; actual message delivery has not been tested here.

Broker closes still use the existing `trade.decision.made.v1` consumer, platform-linked
position checks, LIVE mode, authority and fencing. No canonical outcome/P&L is fabricated
by the timer. The broker-only Live view is unchanged by this correction.

Expanded validation: **279 passed, 23 skipped, 20 subtests passed**, including theoretical
deadlines, no-fill/no-quote expiry, canonical terminal precedence, duplicate suppression,
crash recovery, stale-quote expiry and lifecycle closure after the decision. PostgreSQL
migration and live NATS/subscriber delivery still require integration verification.
