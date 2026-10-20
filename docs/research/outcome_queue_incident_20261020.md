# Outcome queue stall — 2026-10-20

## Evidence

The supplied read-only report observed the primary database at
2026-10-20T03:26:03Z. Writer control was `RESOLVER_PRIMARY` with an active lease
for `unified-outcome-resolver-5689cd574b-5fhsb`. Successful pending-work attempts
were last recorded around 2026-10-19T22:15:14–20Z.

Pending counts: Context V1 29, Donchian 19, Context V2 18, Liquidity 13,
Kojo V3 2, EMA Pullback 2 (83 total). The report classified 37 as never
attempted, 22 as insufficient data with a candle gap, 19 as other insufficient
data, and five as ambiguous intrabar.

Repeated logs show `CheckViolation` on
`entry_signal_outcomes_resolution_state_check` for
`SIG_0311ef40251b929441a25c16` (EMA Pullback). The library adapter supplied
`resolution_state=CANDLE_GAP` from research status `DATA_GAP_UNRESOLVED`.
Migration 051 permits `INSUFFICIENT_DATA` instead. Each exception aborted the
remaining tick; the failed transaction did not advance the attempt timestamp,
so the signal repeatedly blocked subsequent work. This is a confirmed queue
blocker, not proof of the exact initial outage time or every pending cause.

## Repair

- Map unresolved research gaps to canonical `INSUFFICIENT_DATA`, preserving
  original research status/reason, gap classification and unprocessed-path flag.
- Roll back a failed signal and record a fenced retry attempt, then continue
  the batch. Existing evidence, resolution classification and coverage cursors
  are preserved. New failed work gets `PENDING` runtime state, not an outcome.
- Surface `DEGRADED`, failed signal IDs and exception classes in cycle reports.
  Exception messages/row contents are not copied into the failure record.
- Stop on lost writer authority or inability to persist the failure attempt.

No schema expansion, forced closure, strategy-rule change, broker execution,
or observer identity/image change is required.

## Release and read-only verification

Build the runtime from the merged fix and repin the unified resolver through
GitOps. Keep frozen PA experiment images and identities unchanged.

After rollout, inspect resolver logs for successful cycles, absence of the
constraint error, and any explicit per-signal failures. Compare pending counts,
latest attempt timestamps and recent closures with the baseline. Verify the
previously blocked EMA signal now has a supported state and retained gap
evidence. A changing lease heartbeat or Ready pod alone does not prove progress.

Do not require zero pending signals: missing entry candles, unrecoverable price
paths and intrabar ambiguity remain unresolved unless sufficient causal
evidence becomes available. Keep theoretical results separate from broker
results. Live backlog recovery is not verified by local or CI tests.
