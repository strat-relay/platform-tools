# Phase 6 PostgreSQL reconciliation

This is a read-only audit. Neither state artifact was edited, and no PostgreSQL
import or authority change was performed. The live runner was advancing during
the audit, so evidence was copied to `/tmp/phase6-reconcile.qU9OY5` as a stable
read-only snapshot.

## Reconciliation result

At the captured boundary:

- Full: 182 setups, 99 positions; last successful read `16:17:10Z`.
- Compact: 184 setups, 99 positions, 99 opportunities, 7 symbols; last
  successful read `16:54:41Z`.
- Event history: 3,952 rows through `16:54:41Z`.
- Full state size: 466,237,105 bytes.
- Phase-2 snapshots: 604 rows / 250,725,583 bytes.

The compact artifact is newer than the full artifact. This explains the two
extra compact setups, but it does not explain every remaining semantic mismatch.

### Extra compact setups

| setup_id | symbol/direction | created/setup time | lifecycle | event evidence | opportunity / position | signal / terminal |
|---|---|---|---|---|---|---|
| `731d024f91bbdd88736a` | XAUUSDm / LONG | `2026-09-17T16:30:00Z`; first detect 16:30:29Z | `INVALIDATED_NO_REENTRY` / `SETUP_INVALIDATED_BEFORE_ENTRY` | present; last invalidation 16:54:22.234852Z | none / none | no separate signal record; invalidated; terminal |
| `88c4bec88941d44341b3` | BTCUSDm / LONG | `2026-09-17T16:45:00Z`; first detect 16:45:24Z | `WAITING_FOR_RETRACE` / `WAITING_FOR_RETRACEMENT` | present; last detect 16:54:31.315366Z | none / none | no separate signal record; not invalidated; non-terminal |

Classification: both are `COMPACT_NEWER_THAN_FULL`, not merely representation
differences. They are supported by append-only `SETUP_DETECTED` events.

### Position differences

All 99 economic-position IDs, entry-attempt IDs, opportunity IDs, fill times,
entry prices, stops, targets, and MAE values agree except for one position:

| economic_position_id | field | full | compact | authoritative evidence | classification / authority |
|---|---|---|---|---|---|
| `893dcb99c706b6b61c7a` | status | `TARGET_HIT` | `OPEN` | `TARGET_HIT` at 16:54:33.716539Z | `STALE_STATE`; event ledger + full |
| same | `mfe_price` | 203.1079999999929 | 0.0 | target event at 16:54:33.111548Z | `STALE_STATE`; event ledger + full |
| same | `realized_R` | 0.1026397782837697 | null | target event at 16:54:33.716539Z | `STALE_STATE`; event ledger + full |
| same | `exit_timestamp` | 1789656900 | null | target event family | `STALE_STATE`; event ledger + full |
| same | `exit_reason` | `TARGET_HIT` | null | target event family | `STALE_STATE`; event ledger + full |

`size`, `realized_pnl`, `MFE_R`, and `MAE_R` do not exist in either artifact.
`entry`, `original_stop`, `current_stop`, `opened_at`, and `closed_at` must be
derived only where an explicit source supports them; unavailable values remain
null.

### Setup/opportunity differences

The shared setup `c7062322b5a4d4754640` has the same semantic lifecycle in both
artifacts (`RETURN_AFTER_SETUP_TARGET_COMPLETED`); only `m5_start_index` differs
(`302` vs `295`), classified `DERIVED_FIELD_DIFFERENCE`. Setup
`86ccd2396f13aa9df4bd` is material: full says `FILLED`, compact says
`INVALIDATED_NO_REENTRY` with `zone_left=true` and `thesis_invalidated=true`.
Later event history contains filled/target/stopped transitions, so this is
`STALE_STATE` or a checkpoint-ordering `BUG`, not a representation difference.

All 99 opportunity IDs and economic-position links exist in both projections.
Repeated scale-in decisions are in the event ledger and are not fully retained
in the compact opportunity object.

## Temporal explanation

The files do not share a logical snapshot. Full state ends at 16:17:10Z while
compact and the event stream reach about 16:54:41Z. Events between those
boundaries include both extra setups, invalidations, repeated fills/targets,
and the `893d...` position transition. This explains the count difference, but
compact still reporting `893d...` as open after its own boundary is an
import-blocking checkpoint consistency defect.

## Restart-state classification

| Category | State |
|---|---|
| DECISION_CRITICAL | freeze/version identity, symbol cursors, setup identity/qualification, retrace state, entry level, target-consumed and invalidation state, unresolved opportunities, open positions, stop/target, entry mechanism, reentry eligibility |
| RESTART_CRITICAL | runner status/kill switch, seven symbol rows, active/non-terminal setups, unresolved opportunities, open positions, compact context needed by geometry/fill, schema/version/hash identity |
| OBSERVABILITY_ONLY | read health, source age, publisher/consumer diagnostics, gap-recovery telemetry, post-decision MFE/MAE telemetry |
| DERIVED | reports, aggregates, lifecycle summaries, normalized R metrics where reconstructible, `m5_start_index` unless exact replay proves necessary |
| HISTORICAL_ONLY | completed setup history, full event ledger, phase-2 snapshots, evidence not referenced by active entities |

Continuation tests pass because they exercise the minimum decision-critical
projection with identical future market inputs. They do not prove asynchronous
historical and observability fields are equal.

## PostgreSQL row-by-row audit

The current schema is anti-blob: there is no `state_jsonb` checkpoint column,
and the importer does not insert the 466 MB file as one value. The model is not
yet complete enough for a safe import.

| Domain | Full | Compact | Event ledger | PostgreSQL destination | Recommended authority | Row granularity | Reason |
|---|---|---|---|---|---|---|---|
| strategy/config/freeze identity | embedded + manifest | embedded + manifest | repeated metadata | currently runner fields | manifest | one version/manifest | immutable replay identity |
| runner state | runner fields | runner fields | not authoritative | `strategy.phase6_runners` | compact after parity | one runner | bounded only |
| symbol progress | map | seven rows | gap evidence | `strategy.phase6_symbol_progress` | compact | runner/symbol | restart cursors |
| setups | 182 rows | 184 rows | setup events | `strategy.phase6_setups` | event ledger + reconciled state | one setup | lifecycle state |
| setup lifecycle | mutable summary | partial | setup-tagged events | `strategy.phase6_setup_lifecycle` | event ledger | one transition | append-only history |
| opportunities | nested | 99 rows | fills/scale events | `strategy.phase6_entry_opportunities` | compact after parity | one opportunity | attempt identity |
| economic positions | 99 rows | 99 rows | fill/target/stop events | `strategy.phase6_economic_positions` | event ledger + state | one position | durable position state |
| immutable context | giant nested payload | compact projection | phase-2 evidence | observation + reference tables | content-hash evidence | unique observation | deduplication |
| read/gap telemetry | repeated | partial | 2,171 unattached rows | currently dropped | telemetry/archive | one event | must not disappear |

The importer deduplicates compact snapshots by content hash and streams the
large phase-2 files. The phase-2 snapshot file is historical evidence, not
restart state. A direct canonical context reference on setups would make the
observation query path clearer than the current reference table alone.

## Required changes before import

`IMPORTER_CHANGES_REQUIRED=true`; no changes were applied during this audit.

1. Add normalized `platform.strategy_versions`,
   `platform.configuration_versions`, and `platform.freeze_manifests`, and link
   runner rows to immutable IDs.
2. Formalize `strategy.phase6_runners` as bounded `strategy.runner_state`; do
   not add a whole-state JSONB column.
3. Add relational position fields where sources exist: setup/opportunity links,
   symbol/direction, entry, original/current stop, opened/closed timestamps, and
   bounded size/allocation. Keep unavailable PnL/R metrics null.
4. Preserve events with both setup and position identity in both projections or
   introduce one canonical typed lifecycle table.
5. Route unattached `READ_ERROR`, `DATA_GAP_DETECTED`, and baseline records to
   telemetry/audit/archive instead of silently dropping them.
6. Add import preflight for snapshot boundaries and event-derived terminal
   state; reject mismatches like `893d...` and `86ccd...`.
7. Add restart queries for identity, seven symbol rows, active setups,
   unresolved opportunities, open positions, and only referenced observations.

## Size and startup model

Validation expectations are 1 strategy version, 1 configuration version, 1
freeze manifest, 7 symbol rows, 184 compact setups, 99 opportunities, 99
economic positions, and 3,952 captured events. Operational normalized state is
expected to be a small multi-table working set, not a 466 MB checkpoint.
Historical evidence remains substantial: phase-2 snapshots alone are about
239 MiB, plus the phase-2 ledger and event history. Exact content-hash savings
require a controlled import validation; the architecture separates that
historical footprint from operational restart state.

Startup should load identity, one runner row, seven symbol rows,
active/non-terminal setups, unresolved opportunities, open positions, and only
referenced context observations. It must not load a giant historical blob or
all historical observations.

## Decision

`STATE_DIVERGENCE_EXPLAINED=false` — the time boundary explains the extra
setups but not the remaining semantic conflicts.

`SAFE_TO_IMPORT_POSTGRES=false`
`SAFE_TO_CUTOVER_PHASE6=false`
`LIVE_DATA_IMPORTED=false`
`LIVE_DUAL_WRITE_ENABLED=false`
`AUTHORITY_CHANGED=false`
`BROKER_WRITES=0`
`ORDER_SEND_ATTEMPTS=0`
`SERVICES_RESTARTED=0`

The normalized schema/importer changes are now implemented on the feature
branch, but those implementation changes do not resolve the source-artifact
divergence. The preflight therefore continues to reject the current live
artifacts until a coherent explicit boundary is reconstructed.
