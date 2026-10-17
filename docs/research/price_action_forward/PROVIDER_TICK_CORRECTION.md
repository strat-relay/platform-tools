# PA provider tick correction: pa-provider-tick.v1

## Confirmed evidence

Audit captured 2026-10-17T16:26:16.369666Z, original Engulfing instance
`7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204`:

- All 17 retained evaluator streams used event tick_size=1.0.
- Active universe was three instruments, not 17. BTCUSD had 303 evaluations,
  USDJPY 225, XAUUSD 217. Fourteen streams were historical retained state.
- Provider metadata: XAUUSD=0.001, USDJPY=0.001, BTCUSD=0.01.
- XAUUSD had 40 body-size rejections and USDJPY 32. Other decisions failed
  trend, pullback, engulfing or R checks. There were zero accepted entries and
  zero canonical signals, so no accepted-entry publication backlog was found.
- Tick distortion also changes structural-zone clustering/tolerance, stop buffers
  and entry-gap protection. It is not enough to rerun the body-size check alone.

The shared PA event builder defaulted missing row ticks to 1.0 while the managed
runtime left symbol metadata separate from candle rows. The same input path feeds
S/R Retest. Its affected count/outcomes have not been measured by this audit.
Zero signals is not proof of a working correctly configured strategy, nor proof
that every rejection would become a trade with corrected metadata.

## Correction and boundaries

The runtime reads explicit finite positive `symbol_info.tick_size` and copies it
onto H1/M15 input rows without modifying the shared cache. Bool, string, missing,
zero, negative, nonfinite and row/provider conflicts fail closed. `point`, digits,
trade_tick_size and symbol-name heuristics do not substitute for missing tick_size.
The PA event builder no longer has a default.

Every corrected checkpoint binds the input policy, full manifest, adapter sources,
and each instrument's tick size. Legacy checkpoints, conflicting event metadata,
policy/source/manifest changes, and later provider tick changes require a separate
managed instance. No checkpoint, signal or outcome is rewritten. Runtime startup
rejects both original frozen instance IDs. Do not repin their workloads to this image.
The frozen evaluator and parameter values are unchanged. Both completed-bar and
next-open modes carry input policy/tick provenance into research entries and
canonical metadata. Broker execution remains disabled.

## Prepared cutover requirements (not executed)

1. Retain/checksum the original manifest, immutable image, full checkpoint and
   append-only event/outcome ledgers. Keep resolving original theoretical outcomes;
   do not delete/relabel old signals or present the affected interval as validation
   of the intended tick-scaled definition. Preserve its original attribution.
2. Create new managed instance UUIDs referencing each original frozen strategy
   version and its matching frozen parameter set. Do not clone a generic version
   based on display name. Explicit universe: XAUUSD, BTCUSD, USDJPY.
3. Populate managed attributes below. Use the actual frozen parameter-set reference
   and fingerprint from the joined parameter_set row. Parameter fingerprints are
   unchanged by this input wiring repair. Set execution_mode=OFF and
   execution_eligible=false on the instance. Register membership through the normal
   managed-catalog path; no migration number is reserved by this change.

```json
{
  "status": "RESEARCH",
  "mode": "OBSERVER_ONLY",
  "market_input_policy": "pa-provider-tick.v1",
  "signal_publication": true,
  "publication_mode": "ORCHESTRATOR_RESEARCH_ONLY",
  "publication_activation_time": "REPLACE_WITH_ACTUAL_INTEGER_UTC_T0",
  "parameter_set_id": "EXACT_JOINED_PARAMETER_SET_REFERENCE",
  "broker_writes": false
}
```

4. Freeze new manifests with a chosen UTC T0 and
   `market_input_policy: pa-provider-tick.v1`. For immediate entry observation add
   `entry_mode: CONTEMPORANEOUS_CANDLE_OPEN` and configure PRICE_ACTION_ENTRY_MODE
   accordingly. Recompute manifest_hash after finalizing fields. The example above
   intentionally is not deployable until the actual integer T0/reference are supplied.
5. Build/pin one immutable image in distinct GitOps observer workloads with the new
   PRICE_ACTION_INSTANCE_ID values. Use research-only DB credentials. Keep old
   workloads on their original image while a separate reviewed cutover holds them;
   do not run both as one evidence sample. This source change does not scale or stop
   either workload, update database registration, or activate a deployment.
6. Check the first persisted checkpoint: policy and binding present, event ticks
   match provider metadata for each instrument, no mixed ticks, warmup emits no
   entries, fresh processing continues, and the new IDs are discovered by the
   orchestrator. Corrected adapters require explicit activation/fingerprint and
   input provenance; legacy adapters remain available to drain original evidence.
   Research-only canonical signals cannot create execution intents.
7. Verify the first accepted entry's geometry, provider tick provenance, explicit
   parameter set, timing class, publication and theoretical outcome. No minimum
   signal count is promised. Missing signals still need rejection-stage evidence.

Rollback means stopping the new run and retaining its ledgers separately. Never
restore an affected checkpoint into corrected code or merge the two evidence sets.

## Offline comparison tool

`research.replay_pa_tick_correction` accepts a **full exported state**, not the
redacted audit. Both legs use the frozen evaluator. It preserves input files,
compares counts and accepted reference-entry geometry, and labels outputs
RETROSPECTIVE_COUNTERFACTUAL with execution_eligible=false. It has no publisher,
DB, broker, or outcome-writing dependencies. It does not produce validation or
recover retrospective entries as new live signals.

Supply a tick manifest such as `{"XAUUSD":0.001,"BTCUSD":0.01,"USDJPY":0.001}`
only with an explicit applicability assumption. Current tick metadata does not
prove historical tick/revision availability. Specify the original bootstrap M15
count after checking the checkpoint's source history; do not guess T0 from the
number of final evaluations. Historical data revisions and intrabar availability
remain limitations. The tool does not claim original ledger equivalence without
an independently reconciled original replay.

```bash
PYTHONPATH=. python3 -m research.replay_pa_tick_correction \
  --checkpoint full_checkpoint.json \
  --tick-manifest applicable_ticks.json \
  --warmup-m15-bars 320 > tick_counterfactual.json
```

No actual counterfactual trade counts were computed from the summary-only audit.
