# Context post-exit observation ledger

`research/context_structure_retrace_post_exit_ledger.py` is a separate,
research-only observer for `CONTEXT_STRUCTURE_RETRACE_V1`. It reads the
Context compact state and completed M5/M15 bars from the canonical Redis
market-data cache. It only selects positions whose existing lifecycle is
already `STOPPED` and it writes an append-only JSONL ledger outside the
strategy signal/outcome path.

It does not access another strategy, write PostgreSQL, call the MT5 bridge,
change Context entry/stop/target logic, change scheduling, or submit broker
orders. The observation horizon is fixed at 180 minutes after the existing
stop. The exit candle is excluded so a partial candle cannot leak pre-stop
movement into the post-stop result.

Each completed bar observation records:

- favorable and adverse excursion in original-entry R;
- excursion beyond the original stop in R;
- whether the original target was reached after the stop;
- the first causal bar timestamp for target reach.

Each trade record also includes M5 and M15 coverage/summaries and marks the
fixed `<0.25R` diagnostic cohort. That label is descriptive only and is never
used as a production filter or selection rule. Missing cache data produces an
explicit data-gap ledger record rather than an inferred zero.

## Manual research run

```sh
python3 -m research.context_structure_retrace_post_exit_ledger observe \
  --state /work/context_structure_retrace_forward_state_compact.json \
  --ledger /work/context_structure_retrace_post_exit.jsonl
```

The process is intentionally not added to a production scheduler by this
change. If it is later run continuously, it must remain a separately managed
research observer with the same read-only boundary.
