# Unified Outcome Resolver cutover procedure

This procedure is intentionally operational documentation only. It does not
perform a deployment or production write.

## Migration prerequisites

Production currently has the canonical 001–050 schema objects required by the
resolver, although its ledger also contains legacy aliases `19` and `20` for
the zero-padded `019` and `020` migrations. The resolver sequence is:

- `051` requires `strategy.entry_signal_outcomes` from `015`, the expanded
  outcome/status shape from `031` and `037`, and the `platform` schema.
- `052` requires canonical `strategy.entry_signals` from `011` and creates
  the durable cursor, lease, and control-fence tables.
- `053` requires the outcome table from `015` and must follow `051` so the
  versioned resolution columns and trigger semantics are already present.

The safe production prerequisite is therefore a fresh read-only checksum audit,
then the normal ordered migration runner applying `051`, `052`, and `053` in
one controlled transaction. Do not manually insert ledger rows or collapse the
legacy `19`/`20` aliases during this rollout. The production checksums for
`035` and `047` are known compatibility exceptions already encoded by the
migration runner.

## Gates before cutover

1. Apply migrations 051, 052, and 053 in a disposable/staging database and
   verify all constraints, triggers, and indexes.
2. Run the read-only parity audit for Context V1, Liquidity V1, and KOJO V3.
   Every difference must be `MATCH`, an approved semantic correction, or
   explicitly unresolved because evidence is missing. No invented outcomes.
3. Run resolver restart, lease-fencing, lease-expiry, duplicate-event, and
   persist-before-cursor crash tests. Require zero duplicate terminal rows.
4. Confirm every legacy monitor has a shutdown/disable switch and that setup
   evaluation remains enabled. Confirm the Liquidity Live bypass is not active.
   The standalone `monitor_open_liquidity_entries()` and
   `ensure_open_liquidity_outcomes()` calls must be stopped as outcome writers;
   the resolver must already discover both missing and `OPEN` rows. Liquidity
   signals must carry an explicit `outcome_contract` and provider-symbol
   provenance before they are admitted to resolver primary ownership.
5. Before any fence transition, set `OUTCOME_RESOLVER_ENFORCE_WRITER_GATE=true`
   on every legacy writer and verify it can write only while the control row is
   `LEGACY_COMPAT`. The old compatibility SQL path otherwise predates the
   resolver fence and is not safe to leave ungated.

## Ordered cutover

1. Announce a short evaluation freeze and record the current control generation.
2. Stop the Context V1 outcome projector and Liquidity outcome monitor while
   leaving strategy setup/signal production intact. Confirm their processes
   are terminated, not merely idle, and observe zero compatibility writes.
   Any `after_publish` hook that creates an initial outcome row is included in
   this shutdown gate. Signal publication remains allowed, but canonical OPEN
   creation belongs to the resolver and is idempotent with existing OPEN rows.
3. Set the resolver control row to `STOPPED`, verify legacy processes have no
   active canonical writer, then advance the generation to
   `RESOLVER_PRIMARY`. Do not start the resolver before this committed fence
   transition. The resolver must refuse to write under any other mode.
4. Start one resolver deployment. Verify its lease holder, generation,
   heartbeat, cursor advancement, and write counts before enabling a second
   worker. Additional workers must be fenced by the lease.
5. Run the parity sample again, then observe duplicate-write and unresolved-
   evidence metrics through at least one candle cycle.

## Rollback

Stop the resolver first and fence the control row to `STOPPED`. Verify its
lease is expired/released, then set `LEGACY_COMPAT` with a new generation and
start exactly one approved compatibility monitor with the writer gate still
enabled. Never set
`LEGACY_COMPAT` while the resolver can still hold the primary lease. Do not
delete resolver state or outcome columns; the cursor and evidence are required
for a later retry. If parity is uncertain, keep the fence `STOPPED` and leave
signals unresolved rather than running competing writers.
