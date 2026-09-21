import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
src = root / 'artifacts/audits/liquidity_reclaim_continuation_pair_agnostic_20260917.json'
out = root / 'artifacts/audits/liquidity_reclaim_continuation_pair_agnostic_specs_20260917.json'
d = json.loads(src.read_text())
payload = {
    'family': d['family'],
    'research_only': True,
    'locked_boundaries': d['locked_boundaries'],
    'specifications': [
        {
            'SWEEP': {k: x['spec'][k] for k in ('lookback','sweep_atr')},
            'RECLAIM': {'delay_candles': x['spec']['reclaim_delay']},
            'DISPLACEMENT': {'body_atr_min': x['spec']['disp_atr']},
            'STRUCTURE': {k: x['spec'][k] for k in ('bos_delay','structure_lookback','bos_confirmation')},
            'ENTRY_REFERENCE': x['spec']['entry_reference'],
            'ENTRY_DEPTH': x['spec']['depth'],
            'ENTRY_CONFIRMATION': x['spec']['confirmation'],
            'ENTRY_EXPIRATION': x['spec']['expiration'],
            'STOP': {k: x['spec'][k] for k in ('stop_family','atr_buffer')},
            'TARGET': x['spec']['target_r'],
            'MAX_HOLD': x['spec']['hold_minutes'],
            'discovery': x['discovery'],
            'selection': x['selection'],
            'final_untouched_test': x['final'],
            'final_equal_pair_expectancy_R': x['final_equal_pair_expectancy_R'],
            'final_profitable_pairs': x['profitable_pairs_final'],
            'cost_stress_final': x['cost_stress_final'],
            'bootstrap_final': x['bootstrap_final'],
        }
        for x in d['finalist_results']
    ],
    'promotion_status': 'NOT_PROMOTED',
    'note': 'This is a bounded staged research pass. The declared parameter grids are retained in the harness, but this export is not a final global winner or forward-paper candidate.'
}
out.write_text(json.dumps(payload, indent=2) + '\n')
print(out)
