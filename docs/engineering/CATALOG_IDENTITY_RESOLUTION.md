# Catalog identity resolution

The catalog resolves explicitly declared database relationships, not display names,
evaluator names, observed signal counts or deployment-specific UUID constants.

An instance declares its operational strategy using `attributes.strategy_id` or
`attributes.legacy_strategy_id`. If both are present they must agree. Its operational
instance key is `attributes.instance_id`, then `attributes.legacy_instance_id`, then
its registry UUID. These fields are the existing registry import/publication contract.

The registry UUID and both declared instance aliases form its historical signal scope.
Every signal query also filters by the declared strategy ID. Aliases are read on each
request and are not cached or copied into historical signal records. Catalog counts,
outcomes, last events, observed configuration and instance-detail observations/events
use the same scope. Membership/control settings remain on their existing operational
keys; identity aggregation grants no execution authority.

Conflicting strategy declarations, duplicate alias owners, and aliases overlapping a
different operational instance fail closed with an identity error. Registry-only records
without a declared strategy cannot expand historical signal scope. Their existing
compatibility behavior is retained and marked `UNDECLARED_COMPATIBILITY`; this change
does not replace the platform's pre-existing family/evaluator compatibility mappings.

Historical signals outside an explicit link stay visibly unattributed. In particular,
neither a similar display name nor a shared historical instance string across strategy
IDs is evidence for linking them. Missing relationships must be established as database
metadata by their registration/provisioning workflow, not as a new code exception.

No database rows are deleted or rewritten by this read-model change. No migration,
checkpoint edit, execution-mode change or broker operation is required.
