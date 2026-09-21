# Read-only forensics: `893dcb99c706b6b61c7a`

No source artifact was modified. The live append-only streams continued during
inspection; timestamps below are observations from the current files, not a
claim that the runner was stopped at a common snapshot.

## Timeline

| Timestamp | Source / locator | Event ID | Type | Setup / opportunity / position | Target / reentry | Details |
|---|---|---|---|---|---|---|
| 2026-09-17 14:50:00Z | full/compact state; signal line 95 | `SIG_c1a43ca8a5e51bf4e48434d0` | FILLED / signal | `c7062322b5a4d4754640` / `13678f3b20640132b867` / `893dcb99c706b6b61c7a` | target `76585.32678571428`; INITIAL | BTCUSDm SHORT, entry `76622.208`, stop `76981.5347142857` |
| 2026-09-17 14:55:00Z | Phase 7 V1 state | no Phase 7 event ID for V1 exit | TARGET_HIT | same | consumed; INITIAL | `v1_exit_timestamp=1789656900`, `v1_status=TARGET_HIT`, `v1_realized_R=0.1026397782837697` |
| 2026-09-17 16:21:28.665284Z | Phase 6 JSONL line 2968 | no event ID field | FILLED | same | target unchanged | Gap-recovery/replay emission |
| 2026-09-17 16:23:34.200790Z | Phase 6 JSONL line 2971 | no event ID field | TARGET_HIT | same | consumed; INITIAL | `mfe=203.1079999999929`, `mae=0`, `realized_R=.1026397782837697` |
| 2026-09-17 16:28:13.603261Z | archived Trade Manager line 2601 | `3b01f116...` | POSITION_UPDATED | same | `OPEN` observation | Earlier observer snapshot |
| 2026-09-17 16:37:04.333046Z | archived Trade Manager line 2775 | `d3ac5bd...` | POSITION_UPDATED | same | `TARGET_HIT` | Read-only Phase 7 observation |
| 2026-09-17 17:48:15.209869Z | current Trade Manager line 379 | `cd49a948...` | POSITION_UPDATED | same | `TARGET_HIT` | Latest current Trade Manager evidence inspected |

The Phase 6 ledger then repeats `FILLED`/`TARGET_HIT` pairs during gap
recovery. Those rows have no intrinsic event ID and their `event_time` is the
append/processing time, not the causal market-bar time. They are evidence of
replay/re-emission, not new economic positions.

## Contradiction

Full state says the position is `TARGET_HIT`, with exit timestamp
`1789656900`, target reason, MFE `203.1079999999929`, and realized R
`0.1026397782837697`.

Compact top-level `positions[893d...]` says `OPEN`, with no exit fields,
realized R null, and MFE `0.0`. However, compact nested opportunity
`setups[c706...].opportunities[13678...]` says `TARGET_HIT`, with target
consumed by the parent setup. This is an internal compact-state contradiction,
not simply full-versus-compact timing.

The setup itself is terminal in both representations:
`RETURN_AFTER_SETUP_TARGET_COMPLETED`, `retrace_state=FILLED`,
`target_completed=true`. Its position-level reentry type is `INITIAL`; no
second economic position exists for this opportunity.

## Producer semantics and root cause

The strategy runner owns setup and position lifecycle. In
`context_structure_retrace_forward.py`:

1. `_fill()` appends the opportunity to `setup["opportunities"]` and also puts
   the same logical object into `state["positions"]`.
2. `_process_bar()` mutates the nested opportunity on stop/target, including
   status, MFE/MAE, exit fields, and realized R, then sets the setup target
   flag and appends the lifecycle event.
3. `append_event()` writes the event first and then calls `save_state()`.
4. `save_state()` calls `project_state()`.
5. `project_state()` independently projects nested setup opportunities and the
   top-level `source["positions"]` map. After a projection those are separate
   dictionaries, so later nested mutations do not update the top-level mirror.

Therefore a terminal nested opportunity can coexist with an open top-level
position index. Event-first persistence also permits an event to be ahead of a
checkpoint if interrupted. Gap recovery then exposes the replay/re-emission
pattern seen in the ledger.

Classification: `DERIVED_INDEX_BUG` plus `PARTIAL_WRITE` risk. This is not a
legitimate target/reentry reopening and not a serialization-only difference.

## Authority

| Field | Full | Compact top-level | Event/observer evidence | Recommended canonical value | Confidence |
|---|---|---|---|---|---|
| position status | `TARGET_HIT` | `OPEN` | Phase 6 target events, Phase 7 V1, repeated Trade Manager `TARGET_HIT` | `TARGET_HIT` | High |
| target consumed | setup `true` | nested opportunity/parent `true` | `RETURN_AFTER_SETUP_TARGET_COMPLETED` | `true` | High |
| exit timestamp | `1789656900` | null/top-level; nested compact later has `1789657200` | Phase 7 V1 uses `1789656900` | `1789656900` / 14:55Z | High |
| exit reason | `TARGET_HIT` | null | Phase 7 and Trade Manager | `TARGET_HIT` | High |
| realized R | `.1026397782837697` | null | Phase 6 and Phase 7 | `.1026397782837697` | High |
| MFE | `203.1079999999929` | `0.0` | first V1 terminal state records 203.108; later replay reports 383.128 | preserve causal V1 value; retain later replay as evidence | Medium-high |
| reentry state | `INITIAL` | `INITIAL` | signal and position records | `INITIAL`; no reopen | High |

The later compact nested exit timestamp/MFE reflect replayed processing, not a
new economic position. The first V1 terminal record is the strongest causal
record for the original position; later observations belong in immutable
evidence history.

## Isolated or systematic?

The same top-level-position-versus-nested-opportunity contradiction appears in
five compact setups:

| setup_id | economic position | nested status | top-level status | preflight |
|---|---|---|---|---|
| `86ccd2396f13aa9df4bd` | `d455ab7189a34d9ff522` | STOPPED | OPEN | catches |
| `88c4bec88941d44341b3` | `71437ff4c400c756a8bd` | TARGET_HIT | OPEN | catches |
| `c7062322b5a4d4754640` | `893dcb99c706b6b61c7a` | TARGET_HIT | OPEN | catches |
| `c816435d284457644833` | `0093b12995eced6577f4` | OPEN | OPEN | does not catch MFE-only drift |
| `f48c0738477748e8f463` | `4b7f99cca79704bd4dca` | TARGET_HIT | OPEN | catches |

This is systematic, not an isolated historical artifact. The current strict
preflight catches the terminal cases through event evidence but should also be
extended later to compare nested and top-level mutable projections directly.

## Continuation equivalence

The contradiction does not change future strategy decisions because the setup
is already target-consumed and the economic position is terminal. The runner's
future action path does not reopen the same economic position; the later setup
return is represented as `RETURN_AFTER_SETUP_TARGET_COMPLETED`, not as a new
entry for this position. The continuation harness uses the nested
decision-critical projection and identical future market inputs, so it passes.
That does not make the historical top-level index safe to import.

## Repair recommendation

`RECOMMENDED_REPAIR_TYPE=MULTIPLE_ACTIONS_REQUIRED`.

- General producer fix: make the top-level position index a derived view of
  setup opportunities at checkpoint time, or update both projections through a
  single mutation path; add an invariant that they are equal after every save.
- General replay fix: assign stable causal event identity and suppress
  duplicate terminal emissions during gap recovery.
- Historical handling: do not rewrite the immutable event stream here. Import
  should retain the replay rows as evidence and reject inconsistent mutable
  snapshots until a general reconstruction rule is approved.

`BUG_LOCATION=context_structure_retrace_forward.py:_process_bar/_fill` and
`context_structure_retrace_compact_state.py:project_state`.

`FAILURE_MODE=denormalized mutable mirror diverges after nested mutation; event
append and checkpoint write are not atomic; gap recovery re-emits terminal
transitions.`

`TEST_REQUIRED=round-trip every lifecycle event, compare nested opportunities
to top-level positions after each checkpoint, and replay gap recovery twice with
an assertion of one terminal economic transition.`

## PostgreSQL consequence

The eventual normalized rows should be:

- setup `c706...`: one row, `RETURN_AFTER_SETUP_TARGET_COMPLETED`, target
  consumed true;
- lifecycle: preserve original/replayed Phase 6 events plus Phase 7/Trade
  Manager observations as evidence, without treating duplicate replays as new
  economic transitions;
- opportunity `13678...`: one row, `INITIAL`, target and stop preserved;
- economic position `893d...`: one row, `TARGET_HIT`, entry `76622.208`,
  original/current stop `76981.5347142857`, target `76585.32678571428`,
  opened `14:50Z`, closed `14:55Z`, realized R `.1026397782837697`;
- target state: owned by the setup/position fields above, not runner state;
- reentry state: setup owns eligibility; this position remains `INITIAL` and
  terminal.

No repair or import was performed.
