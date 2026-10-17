"""Provider tick contract for corrected PA observation runs."""
import math

POLICY = 'pa-provider-tick.v1'


def tick_size(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('explicit positive finite provider tick_size required; no fallback')
    return float(value)


def bind_rows(rows, provider_tick):
    tick = tick_size(provider_tick)
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('market candle must be an object')
        if 'tick_size' in row and tick_size(row['tick_size']) != tick:
            raise ValueError('row tick_size conflicts with provider metadata')
        result.append({**row, 'tick_size': tick,
                       'market_input_policy': POLICY,
                       'tick_size_source': 'CANONICAL_SYMBOL_METADATA'})
    return result


def validate_checkpoint(state, *, binding=None):
    if state.get('market_input_policy') != POLICY:
        raise ValueError('legacy/unbound PA checkpoint; preserve it and start a new managed instance')
    if binding is not None and state.get('market_input_binding') != binding:
        raise ValueError('PA source/manifest binding changed; start a new managed instance')
    ticks = state.get('instrument_tick_sizes')
    if not isinstance(ticks, dict):
        raise ValueError('checkpoint requires explicit instrument tick bindings')
    for instrument, value in ticks.items():
        tick_size(value)
    for key, value in state.get('evaluators', {}).items():
        instrument = key.split('|', 1)[-1]
        if instrument not in ticks:
            raise ValueError('unbound checkpoint evaluator instrument')
        for event in value.get('snapshot', {}).get('events', []):
            provenance = event.get('provenance') or {}
            if provenance.get('market_input_policy') != POLICY:
                raise ValueError('checkpoint contains legacy event provenance')
            if tick_size(provenance.get('tick_size')) != ticks[instrument]:
                raise ValueError('checkpoint event tick metadata conflicts with binding')
