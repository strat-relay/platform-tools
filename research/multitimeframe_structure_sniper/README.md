# MULTITIMEFRAME_STRUCTURE_SNIPER_RESEARCH

Research-only family. It is not registered with production strategy runners,
the frozen V1 adapter, broker routing, or execution state.

The engine models H4/H1 structure and scenario hypotheses, M15 confirmation,
and M5 entry precision. All detectors require an `as_of` boundary and consume
only completed candles. `external_cases.jsonl` contains observed facts only;
it is never used for parameter selection.

Historical-data readiness is intentionally separate from implementation. No
large replay or optimization is started by this package.
