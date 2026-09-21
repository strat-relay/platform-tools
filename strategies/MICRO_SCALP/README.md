# MICRO_SCALP

Status: completed historical research branch; do not optimize or forward-run.

## Design

This branch tested small-account XAUUSDm scalping using recent liquidity
sweeps, reclaim/confirmation, micro structure, tight structural stops, and
short holding times. It was deliberately separated from the baseline and from
LIQUIDITY_DISPLACEMENT_SCALP_V1.

## Historical commands

```sh
cd /Users/caleb/mt5-native-bridge
python3 micro_scalp_audit.py
python3 micro_scalp_edge.py
python3 micro_scalp_filter.py
python3 micro_scalp_validate.py
```

These scripts are historical research scripts, not a live or continuous
runner. They write the canonical setup, edge, filter, and summary artifacts at
the repository root; see [output README](output/README.md).

## Research conclusion

The canonical setup set was useful for sizing and outcome analysis, but the raw
edge was effectively break-even and out-of-sample filtering was negative.
Minimum-lot constraints made a roughly $100 account impractical. The branch is
archived as evidence, not a deployable strategy.

## Constraints / future use

Do not change this branch while comparing newer strategies. Any new hypothesis
must be a new named research branch with a fresh chronological split.
