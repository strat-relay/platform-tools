# Canonical StrategySignal

`orchestration.models.StrategySignal` is frozen/immutable and uses schema
`strategy-signal-v1`. Its identity is derived from strategy ID/version,
strategy instance, economic position ID, entry opportunity ID, and source
event ID. Re-reading the same native event therefore produces no duplicate
signal.

It contains strategy opportunity data and provenance, but never account
balance, equity, lot size, subscriber data, or broker credentials. Account
and sizing information is downstream in `AccountSnapshot` and
`SizingDecision`.
