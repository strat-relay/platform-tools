"""Explicitly identified PA next-open mode; never resume a frozen legacy run."""
from research.price_action_forward_observer import Observer, append_jsonl
from core.strategies.entry_timing import MAX_ENTRY_DELAY_SECONDS
from research.price_action_metadata import POLICY, validate_checkpoint
from dataclasses import replace
from strategy_backtest.next_open import observe_price_action_open


class NextOpenObserver(Observer):
    def ingest_open(self, instrument, broker_symbol, opening, *, observed_at, snapshot_id):
        validate_checkpoint(self.state, binding=self.input_binding)
        emitted = []
        if opening is None or not 0 <= opening.observed_at-opening.open_time <= MAX_ENTRY_DELAY_SECONDS:
            return emitted
        strategy = self.strategy_filter
        key, evaluator = self._evaluator(strategy, instrument)
        execution = evaluator._execution_events.get(instrument, [])
        if not execution or execution[-1].close_timestamp != opening.open_time:
            return emitted
        for evaluation in observe_price_action_open(evaluator, opening,
                setup_available_at=opening.observed_at, now=opening.observed_at):
            evaluation = replace(evaluation, provenance={**evaluation.provenance,
                'market_input_policy': POLICY,
                'provider_tick_size': self.state['instrument_tick_sizes'][instrument],
                'tick_size_source': 'CANONICAL_SYMBOL_METADATA'})
            row = {'strategy': strategy, 'instrument': instrument, 'broker_symbol': broker_symbol,
                   'observed_at': observed_at, 'source_kind': 'LIVE_MARKET', 'event': evaluation.to_dict()}
            emitted.append(row)
            append_jsonl(self.events_path, row)
            if (evaluation.provenance.get('rejection_reason') == 'ENTRY_OBSERVED_RESEARCH_ONLY'
                    and not evaluation.provenance.get('entry_cancelled')):
                self._register_entry(strategy, instrument, evaluation, snapshot_id)
            state = self.state['evaluators'][key]
            state['evaluations'] += 1
            reason = evaluation.provenance['rejection_reason']
            state['rejections'][reason] = state['rejections'].get(reason, 0)+1
        self.state['evaluators'][key]['snapshot'] = evaluator.snapshot_state()
        self._save()
        return emitted


def mode_binding(manifest, instance_id, strategy):
    """Bind the new adapter/policy and full manifest without altering frozen V1 IDs."""
    import hashlib
    import json
    from pathlib import Path
    from core.strategies import entry_timing
    from strategy_backtest import next_open
    from research import price_action_metadata
    files = [Path(__file__), Path(entry_timing.__file__), Path(next_open.__file__), Path(price_action_metadata.__file__)]
    payload = {'instance': instance_id, 'strategy': strategy,
               'manifest': json.loads(Path(manifest).read_text()),
               'sources': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
