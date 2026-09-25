# Historical V2 attempt quarantine

The 28 production `execution_v2.execution_attempt` rows created on 2026-09-23/24 remain in
their original `SENDING` state because no immutable bridge acknowledgement or broker identifier
can be established for them. They are not rewritten to `FAILED`, `NOT_SENT`, `SENT`, `SUCCESS`, or
`RETRYABLE`, and no `execution_result` is manufactured.

Migration `022_execution_attempt_quarantine.sql` records each existing `SENDING` row in the
separate `execution_v2.execution_attempt_quarantine` relation with:

- `reason=HISTORICAL_AMBIGUOUS_EXECUTION`
- `disposition=RECONCILIATION_REQUIRED`
- migration provenance and the original state/generation/timestamp

The worker checks this relation before ownership acquisition, fencing, canary reservation, or
bridge submission. A redelivery, worker restart, or later authority transition cannot dispatch a
quarantined attempt. The original attempt row remains auditable and unchanged.

## Historical cause

The original V2 worker path was introduced in `37520c28`. Its `process_signal` path created an
eligible intent and proceeded toward an execution attempt without the canonical authority check.
The PostgreSQL authority control-plane guard was added in `9c22a4b`; current code checks the
canonical authority before creating an attempt and records a blocked intent when authority is
disabled. The historical rows were created during the earlier implementation/deployment window,
not by the current guarded path.
