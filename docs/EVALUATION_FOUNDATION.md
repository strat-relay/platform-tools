# V1.1 evaluation foundation

The platform now has one immutable, transport-independent representation of a
strategy decision: `core.strategies.evaluation.Evaluation`.

## Canonical record

An `Evaluation` contains strategy identity, optional version and parameter-set
identity, instrument, direction, explicit `decision_time`, candidate identity,
one of `SIGNAL`, `REJECT`, `NO_CANDIDATE`, or `REVIEW_REQUIRED`, ordered
`DecisionTrace`, reason-code references, trace fidelity, runtime/evaluator
versions, and provenance hooks (`as_of`, `data_digest`, and `market_source`).

`DecisionTrace` preserves ordered immutable `StageResult` records. A stage can
carry observed and expected values, a margin, evidence timestamps, a primitive
identifier, structured metadata, and a versioned reason code. `PASS`, `FAIL`,
and `NOT_EVALUATED` are intentionally distinct.

## Fidelity

- L0: final outcome only; no reliable stage evidence.
- L1: coarse legacy event or lifecycle evidence.
- L2: legacy detector evidence for observed stages, with gaps.
- L3: complete ordered stage evidence from a canonical runtime.

The Liquidity adapter is L2 because its unmodified detector exposes candidate
fields only when a candidate exists. It does not infer failed stages when the
legacy detector returns `None`. The Context adapter is L1 because it translates
existing lifecycle records without reconstructing the original state machine.

## Reason codes and serialization

The small `v1` registry includes only codes used by the adapters and generic
runtime outcomes, including `NO_CANDIDATE`, `RETRACEMENT_NOT_FILLED`,
`STRUCTURE_INVALIDATED`, `DATA_UNAVAILABLE`, and `REVIEW_REQUIRED`. Unknown
codes fail closed. References carry both `code` and `version`.

Canonical JSON uses UTF-8, sorted keys, compact separators, explicit schema
version `strategy-evaluation.v1`, and normalized timezone-aware datetimes.
`Evaluation.evaluation_hash` and `DecisionTrace.trace_hash` are SHA-256 over
those canonical bytes.

## Legacy example

For a Liquidity Displacement result with sweep, displacement, micro-structure
shift, and retracement fill, the adapter emits `SIGNAL` with four ordered
passing stages. If the same candidate has status `UNFILLED`, the adapter emits
`REJECT` with `RETRACEMENT_NOT_FILLED`. If the frozen detector returns no
candidate, it emits `NO_CANDIDATE` and marks the legacy detector stage
`NOT_EVALUATED`; it does not invent a sweep failure.

No legacy strategy source is modified. The adapters do not connect to MT5,
PostgreSQL, NATS, HTTP, or Strategy Studio.

V1.2 may persist these canonical bytes and hashes in PostgreSQL and transport
them through NATS/JetStream. This phase intentionally creates neither.
