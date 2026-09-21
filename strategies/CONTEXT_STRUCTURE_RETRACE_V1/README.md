# CONTEXT_STRUCTURE_RETRACE_V1

Status: frozen V1 prospective paper collection. No live execution exists.

## Design

This isolated branch causally describes the chart a discretionary trader may
inspect: M5/M15/H1/H4 context, completed and forming higher-timeframe candles,
ATR-normalized S/R zones, EMA20/50/100/200 context, pattern events, trendline
and channel candidates, raw candidates versus a compact attention layer, and
separate future outcome labels.

The interface accepts a supplied symbol and timeframe configuration. It must
remain instrument-agnostic: no XAU/BTC/USDJPY strategy branches or absolute
price thresholds are allowed. Structural comparisons use ATR, percentages,
time, R, or broker metadata.

## Research and forward commands

```sh
cd /Users/caleb/mt5-native-bridge
python3 -m unittest test_context_structure_retrace_phase1.py test_context_structure_retrace_phase2.py -v
python3 context_structure_retrace_phase2.py --data /tmp/context-structure-retrace/xau.json --symbol XAUUSDm --start 2026-03-01T00:00:00Z --end 2026-06-30T23:59:59Z
python3 context_structure_retrace_cross_sanity.py
python3 context_structure_retrace_forward.py health
python3 context_structure_retrace_forward.py report
```

The Phase 2 command writes a descriptive ledger, CSV index, JSON results, and
Markdown summary. It does not generate trades.

## Provenance and constraints

- Completed candles are required for pattern events.
- Forming HTF candles are rebuilt only from lower-timeframe bars available at the timestamp.
- Future outcome labels are generated after feature construction and are never passed into rankings.
- March–June and July–September 2026 were previously exposed by earlier research; they are development/research periods, not untouched validation. A future post-freeze period is required for a true holdout.
- Trendline/channel detectors return candidates and do not declare a trade-relevant structure.

## Future plans

Phase 1/2 representation and Phases 3–5 lifecycle audits are retained as
development evidence. The frozen prospective runner is documented in
`docs/strategies/CONTEXT_STRUCTURE_RETRACE_V1.md` and `docs/OPERATIONS.md`.
