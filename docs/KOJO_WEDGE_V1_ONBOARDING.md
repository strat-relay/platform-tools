# KOJO_WEDGE_V1 onboarding decisions

This is a framework-onboarding input, not an evaluator. All decisions below are `CHAT_DECISION` from the Kojo formalization handoff. No StrategyInstance, membership, risk route, or execution route is created by recording them here.

## Frozen V1 scope

- StrategyVersion: `KOJO_WEDGE@V1` — `CHAT_DECISION`.
- Scope: XAUUSD on H1 — `CHAT_DECISION`.
- Parallel corrective channels do not qualify; convergence is required — `CHAT_DECISION`.

## Geometry

- Use confirmed swing pivots — `CHAT_DECISION`.
- Upper boundary requires at least two swing highs — `CHAT_DECISION`.
- Lower boundary requires at least two swing lows — `CHAT_DECISION`.
- At least four alternating pivots are required in total — `CHAT_DECISION`.
- Pivot strength is a parameter, not an optimized constant — `CHAT_DECISION`.

## Breakout and entry

- Breakout requires a completed candle close beyond the relevant wedge boundary — `CHAT_DECISION`.
- A wick beyond the boundary is insufficient — `CHAT_DECISION`.
- No mandatory retest in V1 — `CHAT_DECISION`.
- Entry is `MARKET` at the next candle open after confirmed breakout — `CHAT_DECISION`.

## Stop and target

- Stop is beyond the opposite-side most recent structural swing within the wedge — `CHAT_DECISION`.
- Safety buffer is parameterized; no arbitrary optimized buffer is frozen — `CHAT_DECISION`.
- Target is the nearest confirmed pre-entry structural swing in trade direction — `CHAT_DECISION`.
- Target must be outside the wedge — `CHAT_DECISION`.
- No fixed-R target and no minimum-R filter in the faithful baseline — `CHAT_DECISION`.

## Lifecycle

- An opposite boundary break before confirmation invalidates the candidate — `CHAT_DECISION`.
- A valid breakout consumes the opportunity — `CHAT_DECISION`.
- The evaluator must not chase a later entry — `CHAT_DECISION`.
- Maximum wedge age is parameterized — `CHAT_DECISION`.

## Still required by the evaluator PR

The following are implementation parameters/definitions, not invented here: confirmed-pivot algorithm, boundary fitting, convergence tolerance, structural-swing selection, safety-buffer value range, maximum age value, and exact signal/outcome provenance fields. The next Kojo PR must encode them as a `ParameterSchema` and prove historical/live parity before any runtime adoption.
