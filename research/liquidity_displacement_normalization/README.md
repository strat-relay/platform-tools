# Liquidity-displacement event normalization

Read-only historical research package. It normalizes nested sweep
observations into market events, sweep sequences, entry opportunities, and
entry attempts without changing any forward runner or execution behavior.

Run the replay from the repository root:

```bash
python3 -m research.liquidity_displacement_normalization.replay
```

Run its tests:

```bash
python3 -m unittest research.liquidity_displacement_normalization.test_normalization
```

Outputs are written in this folder:

- `liquidity_displacement_normalization_results.json`
- `liquidity_displacement_normalization_observations.csv`
- `liquidity_displacement_normalization_summary.md`

The frozen strategy remains at the repository root and is not modified.
