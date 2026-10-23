# Generic execution eligibility — draft, not activated

Remove the executor's source-code list of research-only strategy IDs. Eligibility
must follow records and explicit identity links, not a strategy's name or evaluator.
Strategy identifiers remain necessary for attribution and database policy lookups;
this change does not remove adapters or rewrite their rules.

## Boundary

The canonical intent factory still validates the signal's immutable LIVE mode and
revision before proceeding. Immutable research-only, shadow-only, execution-ineligible,
production-ineligible and retrospective evidence cannot be overridden by changing
today's configuration.

For a prospective LIVE signal, load exactly one managed instance by registry UUID or
explicit publication/legacy alias, within the same declared strategy. Strategy ownership
comes from instance `strategy_id`/`legacy_strategy_id` metadata, or an exactly matching
registry family key; there is no evaluator/name inference. Conflicting declarations or
multiple matching instances are rejected. Both the instance and operational parent must
be enabled, the instance must be LIVE and explicitly execution-eligible, and its current
mode revision must match the captured revision. Research restrictions on the instance
and operational parent still block.

Missing policy, malformed data, missing tables and insufficient privileges never grant
permission. The query locks the selected instance and parent with FOR SHARE for the intent
transaction; rollout must verify the worker role's required select/locking permissions.
No grants or role changes are included here.

Global authority, configured account/strategy/symbol allow-lists, risk, sizing, stale-signal
checks, ownership leases, fencing and broker reconciliation remain independent and intact.
Passing this policy is not permission to submit an order.

## Rollout boundary

This is an unapplied draft. No migration, record update, deployment or live broker check
is included. Existing records without explicit registry ownership/eligibility will stop
with a specific policy reason, not silently inherit authority. Before any rollout, an
operator must review identity coverage, immutable provenance coverage, query permissions,
mode-revision refresh behavior and isolated PostgreSQL tests. Do not backfill approval
onto historical signals or disable database guards to make tests pass.

The diagnostic CLI no longer imports an executor strategy-ID denylist. Its configuration
summary is not the canonical policy verdict and still requires runtime preflight.
