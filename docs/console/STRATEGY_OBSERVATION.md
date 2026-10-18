# Strategy observation read model

The strategy instance detail owns Observation. The console's standalone `/observers` view is removed; its old URL redirects to `/strategies`. The older API remains for compatibility.

## Read contract

- `GET /api/v1/strategies/{strategy}/instances/{instance}/observation`
- `GET /api/v1/strategies/{strategy}/instances/{instance}/observation/evaluations/{event}`

Filters: `period=RUN|TODAY|24H|7D|CUSTOM`, explicit timezone-aware `from`/`to` for CUSTOM, `instrument`, owning `run_id`, `offset>=0`, `limit=1..100`. Dates use persisted observation time and a half-open `[from,to)` UTC window. RUN uses the persisted run T0 or earliest recorded observation. Current checkpoint health is independent of dates. Each query uses a repeatable-read, read-only transaction with an eight-second statement timeout. Instance ownership is checked before reading its evidence.

Full-range counts and bounded page results are separate queries. Canonical linkage is limited to the owning strategy/instance and recorded source event or evaluation identity. Publication comes from the linked outbox status; it is not inferred from an accepted entry or canonical row. Counts describe the current handoff state of the evaluation cohort, not events grouped by publication time. They do not describe broker fills or outcomes.

PA and library observers expose their persisted evaluation ledgers. Other strategies expose only canonical evaluations explicitly bound to that instance; coverage is PARTIAL, with unknown setup/accepted/pending counts returned as null. No detector decision is invented. Active PA coverage comes from the latest cycle; old retained instrument streams are separate. Library coverage comes from its frozen run manifest. Runs cannot be combined across instances; the run selector currently exposes the owning persisted run only.

## Evaluation evidence

Qualification describes that recorded evaluation, not an assertion that an old proposal remains pending. Later episode events are joined only by explicit candidate identity within the same source/run/instrument. They may fall outside the selected date range. Publication and recorded execution eligibility are independent sections.

Stages, parameters, timestamps and geometry come from retained records. A candle chart is shown only for PA records whose input snapshot identity and reference cutoff still match retained completed M15 inputs. No future candles or current market reads are used. A body-size diagnostic may be derived from that matched input and recorded parameters; it is labeled derived rather than a persisted stage result. Other missing evidence is explicitly unavailable. Input-contract warnings apply to unverified historical runs and do not rewrite their records.

## Rollout and validation

Deploy the platform API before the console. An unavailable endpoint/source displays unavailable evidence, never fabricated zero counters. Existing read-role grants must cover the research ledgers, canonical signals/outbox, catalog and instrument mappings; no grants or migrations are included here. Review existing ledger size and query latency on rollout; timeout failures remain explicit rather than returning partial counts. No workload, observer, strategy rule, publication, execution or outcome behavior is changed.

Validation: scoped API tests cover ownership, UTC boundaries, page-independent counts, run isolation, partial coverage, and causal evidence. SQL was exercised against a local PostgreSQL-compatible engine; live cluster permissions and performance were not checked from this environment. Console tests exercise filters, nested field conversion, stale-scope clearing, and missing-evidence rendering.
