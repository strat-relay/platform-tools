# Strategy library forward observation — candidate integration

This is the first integration phase for the 44 new library compositions. It does
not replace the five existing Price Action evaluators, Liquidity, Context or Kojo.
The three Price Action reversal evaluators still need their own frozen-evaluator
runtime adapter. No changes to their existing deployments or historical IDs.

## Delivered boundary

`python -m research.strategy_library_forward` reads only the canonical Redis
cache and writes only new research tables. It imports no signal publisher,
orchestrator adapter, outbox, execution service, account or broker client.
Signals from these research tables must not be routed to accounts or counted
as executable canonical signals. Managed catalog registration is not performed.

One immutable run manifest selects strategy keys and explicit canonical/provider
mappings. One worker can run many definitions; snapshots are reused for streams
with the same instrument/timeframes. A transaction-scoped advisory lock serializes
workers for the same run. State and append-only deduplicated records commit in
one transaction. A database error rolls back the cycle. A stream input rejection
is atomic and leaves that stream's previous state untouched; other healthy streams
may progress. Configuration/source changes require a separately identified run.

Both source artifact and adapter source are hashed into the immutable run binding.
Saved manifests include resolved definitions. Do not change activation T0 on restart.
A first successful snapshot establishes warm-up only. It cannot emit a setup or
entry from bootstrap, even if the setup meets the rules. Later observed closes are
classified by actual processing delay. Delayed setups are recovery evidence;
accepted entries are always COMPLETED_CANDLE_RECONSTRUCTION, regardless of delay.
The adapter does not add live tick entry logic or claim fills at historical opens.

Full prefixes are retained to preserve EMA seeds and as-of equivalence. Do not
truncate and re-seed silently. The explicit 10,000-bar per-timeframe cap fails
closed; archive/continuation design is needed before reaching that cap. The state
is intentionally not a scalable market-data archive. Measure JSON state size,
cycle time and DB reads before increasing the strategy/instrument batch size.

Expected market closures are not waived inside frozen detector contracts or the
research outcome resolver. An FX weekend or session closure can therefore yield
context-gap rejection or unresolved theoretical outcomes. Changing this requires
an explicit reviewed calendar policy; it is not a collector outage by definition.

## Data readiness

Current cache supports M5/M15/H1/H4. Four definitions requiring D1 are explicitly
blocked: Daily Channel Breakout, Daily EMA Pullback, Weekly Range Break + Retest,
and H4 Momentum Continuation. H4 Structure Pullback requires H1/H4 cache coverage.
VWAP hypotheses require usable tick volume. A positive canonical `tick_size` must be
available; there is no price-digit-derived guess or silent point fallback.
Every stream requires at least the manifest's 320 requested cache bars for both
roles. Actual series lengths, revision consistency, completed timestamps,
OHLC, current watermarks, freshness, gap and recovery state are validated.
Context arriving after execution buffers finalization until the boundary is covered.
Availability at the original close is an assumption when arrival history is absent;
processing time is separately recorded and never inserted as earlier availability.

## Provisioning and canary steps (not executed)

1. Review PR and tests; inspect deployed collector support for required symbols,
   H1/M15 depth, tick metadata and volume. Verify the canonical resolver deployment
   fixes the incident queue before growing the research footprint.
2. Assign an unused migration slot for `schema_candidate.sql` after inspecting the
   live migration ledger. Review table ownership and research-only write grants.
   It is not in the migration directory and cannot be auto-applied by this PR.
3. Copy `canary.template.json` to a run-specific manifest. Replace run ID and null
   activation_time with the agreed UTC activation epoch. Null deliberately fails
   startup. Freeze and archive this file; never edit it during the experiment.
4. Build the runtime image containing this adapter; pin its verified immutable
   digest in a new standalone GitOps workload. Do not repin existing workloads.
   Use one replica, a conservative resource budget, and dedicated research-only
   credentials. Disable Kubernetes token auto-mount; no cluster API is needed.
5. Start Donchian Breakout, EMA Pullback and FVG Retracement on XAUUSD/BTCUSD/USDJPY.
   Readiness must call `health` and wait for successful fresh stream processing.
   Liveness should detect process/cycle failure, not restart repeatedly because
   a market-data stream is degraded. No execution or notification consumer.
6. Verify bootstrap emits zero entries; observe fresh candle evaluations; compare
   restarted and uninterrupted state; reconcile accepted entries and theoretical
   outcomes. Check observer bridge requests=0 and absence of canonical publication.
7. Expand through reviewed manifests. Add EURUSD for EUR Mean Reversion. The full
   library template selects all 44 keys, but is not a deploy-ready all-at-once run.
   D1 strategies remain blocked until the collector/calendar dependency is resolved.

Commands after provisioning and a frozen manifest exist:

```bash
PYTHONPATH=. python -m research.strategy_library_forward readiness --manifest /config/run.json
PYTHONPATH=. python -m research.strategy_library_forward once --manifest /config/run.json
PYTHONPATH=. python -m research.strategy_library_forward run --manifest /config/run.json --interval 60
PYTHONPATH=. python -m research.strategy_library_forward health --manifest /config/run.json
```

`once` and `run` write research state, unlike `readiness` and `health`. They are
not commands to execute against production before provisioning/review. Rollback
scales/stops the new observer through GitOps; preserve all research rows and T0.
There is no broker rollback because the observer has no broker execution path.

## Remaining release work

Provisioned schema/grants, managed research catalog registration, immutable image
and actual GitOps workload are not included or applied. This PR supplies reusable
runtime integration and reviewable configuration/schema candidates. PostgreSQL
transaction/locking tests are included and use the explicitly isolated CI test DSN;
they skip locally without that DSN. CI must pass them before provisioning. Tests
do not establish that the deployed database or cache is ready. Existing
Price Action reversal adapters and portal/report read models are subsequent work.
No strategy graduates from RESEARCH, and no profitability claim is made.

## Canary registration candidate

`research.strategy_library_registration` generates review-only SQL and
`registration_manifest.json` records deterministic definition/version/parameter/
instance UUIDs for the three canary strategies. Parameter references and hashes
match the frozen runtime exactly. Existing records are never updated. A conflicting
identity or safety configuration aborts instead of silently binding wrong rows.
Instances start `online=false`, `execution_eligible=false`, `execution_mode=OFF`.
No orchestrator adapter is registered. This is catalog preparation, not activation.

The legacy schema makes `strategy_definition.family_key` unique, so namespaced
`LIBRARY:<strategy_id>` keys are used for compatibility. Actual family classification
lives in provenance/instance attributes. `strategy_version.lifecycle=FROZEN` means
implementation immutability under the existing schema; research eligibility remains
RESEARCH in metadata. This does not implement the future first-class family or
Deployment domain objects, or rewrite existing definitions.

User-supplied DB snapshot 2026-10-16T17:24Z lists migration 062 and only existing
research tables. Migration versions are text; the DESC LIMIT output is lexical and
does not prove a new slot is free. A full numeric slot check is required before
assigning the candidate SQL to an automatic migration. No migration was added or run.


## Verified provisioning preflight

The user-supplied 2026-10-16T17:35Z preflight reports no numeric migration version
>=063, no matching canary definitions, neither proposed ledger table, and no
strategy_library_observer role. Slot 063 is a candidate, not reserved: recheck it
before promoting any SQL into the automatic migration directory.

`provisioning_candidate.sql` combines schema, three offline registrations and
research-table grants in one explicit transaction. The role is a NOLOGIN group
with no administrative flags and no grants on canonical/execution tables. It
creates no passwords, Secret or login and grants no existing user membership.
Provision a dedicated login bound only to that group separately before activation;
existing broad trading_app credentials are not the intended observer credentials.
Registration is catalog preparation only; online=false and execution_mode=OFF.
The bundle is outside postgres/migrations and has not been executed.

Next release sequence: pass latest PR CI and review/merge #191; obtain the immutable
runtime image; promote the reviewed provisioning SQL with a freshly checked slot
through the normal migration flow; provision dedicated credentials privately;
prepare a standalone GitOps workload pinned to that digest and immutable run/T0;
review that concrete workload before activation. Do not repin unrelated workloads.
