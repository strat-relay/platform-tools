# ExecutionIntent schema

`ExecutionIntent` is immutable and is created only from one prospective
signal plus one `EXECUTABLE` sizing decision. Its deterministic identity
includes signal, sizing decision, portfolio, and account identity.

It carries strategy prices and approved volume, but no broker order ID. Broker
IDs are added only after a future explicitly armed execution phase.

An intent preserves `signal_id`, `strategy_id`, `strategy_version`,
`portfolio_id`, `account_id`, `sizing_decision_id`, and the original account
snapshot ID for attribution and reconciliation.
