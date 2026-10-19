"""Read accepted library entries; the observer never publishes canonical signals.

Completed-candle reconstruction is permanently retrospective evidence, regardless
of discovery latency. The orchestrator remains the sole canonical publisher.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from orchestration.models import StrategySignal, stable_id
from core.strategies.entry_timing import MAX_ENTRY_DELAY_SECONDS
from research.strategy_library_registration import PUBLICATION_KEYS, registration
from strategy_backtest.library.selection import EXPANSION_KEYS
from strategy_backtest.library.selection import resolve_definition


def iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat().replace('+00:00', 'Z')


class StrategyLibraryAdapter:
    strategy_version = 'V1'

    def __init__(self, conn: Any, key: str, instance: dict):
        if key not in PUBLICATION_KEYS:
            raise ValueError('strategy not authorized for library canary publication')
        self.conn, self.key, self.instance = conn, key, instance
        self.identity = registration(key)
        self.strategy_id = self.identity['strategy_id']
        self.definition = resolve_definition(key)
        self.strategy_version = self.definition.version
        if (instance['instance_id'] != self.identity['instance_id'] or
                instance.get('parameter_set_fingerprint') != self.identity['parameter_hash'] or
                not instance.get('orchestrator_run_id')):
            raise ValueError('library publication identity/configuration mismatch')
        if key in EXPANSION_KEYS and (instance.get('execution_mode')!='OFF' or instance.get('status')!='RESEARCH' or instance.get('signal_publication') is not True):
            raise ValueError('expansion publication requires activated research-only OFF instance')
        activation = instance.get('publication_activation_time')
        if type(activation) is not int or activation <= 0:
            raise ValueError('explicit publication activation boundary required')

    def discover_new_signals(self, seen_signal_ids):
        with self.conn.cursor() as cur:
            cur.execute('''SELECT event_id, instrument, payload FROM
                research.strategy_library_forward_event
                WHERE run_id=%s AND strategy_key=%s AND kind='EVALUATION'
                  AND payload->'payload'->'provenance'->'research_entry' IS NOT NULL
                ORDER BY observed_at,event_id''',
                (self.instance['orchestrator_run_id'], self.key))
            rows = cur.fetchall()
        result = []
        for event_id, instrument, record in rows:
            signal = self.convert(event_id, instrument, record)
            if signal.signal_id not in seen_signal_ids:
                result.append(signal)
        return result

    def convert(self, event_id, instrument, record):
        evaluation = record['payload']
        provenance = evaluation['provenance']
        entry = provenance['research_entry']
        if (record.get('execution_eligible') is not False or
                record.get('strategy_key') != self.key or record.get('instrument') != instrument or
                evaluation.get('strategy_id') != self.strategy_id or
                (self.key in EXPANSION_KEYS and evaluation.get('strategy_version') != self.strategy_version) or
                evaluation.get('parameter_set_id') != self.identity['parameter_set_id'] or
                provenance.get('reason') != 'ENTRY_ACCEPTED' or
                provenance.get('execution_eligible') is not False or
                provenance.get('research_only') is not True or
                provenance.get('parameter_hash') != self.identity['parameter_hash'] or
                entry.get('parameter_hash') != self.identity['parameter_hash'] or
                instrument not in self.instance.get('active_instruments', []) or
                entry['instrument'] != instrument or
                entry['strategy_version'] != self.strategy_id+'@'+self.strategy_version or
                entry.get('provenance', {}).get('entry_cancelled') is not False):
            raise ValueError('invalid library entry provenance')
        price, stop, target = (float(entry[k]) for k in ('entry_price','stop_price','target_price'))
        direction = entry['direction']
        if (not all(math.isfinite(x) for x in (price,stop,target)) or
                not ((direction == 'LONG' and stop < price < target) or
                     (direction == 'SHORT' and target < price < stop))):
            raise ValueError('invalid library entry geometry')
        entry_time = float(entry['entry_timestamp'])
        observed = float(record['observed_at'])
        cutoff = float(provenance['information_cutoff'])
        step = {'M5':300,'M15':900,'H1':3600,'H4':14400,'D1':86400}[self.definition.execution]
        live_open = record.get('evidence_class') == 'CONTEMPORANEOUS_CANDLE_OPEN'
        if (not all(math.isfinite(x) for x in (entry_time,observed,cutoff)) or
                entry_time < self.instance['publication_activation_time'] or
                record.get('decision_time') != cutoff):
            raise ValueError('invalid entry timing')
        if live_open:
            timing = provenance.get('entry_timing') or {}
            if cutoff != entry_time or not entry_time <= observed <= entry_time+MAX_ENTRY_DELAY_SECONDS or not timing:
                raise ValueError('invalid contemporaneous entry timing')
        elif not (entry_time + step == cutoff <= observed and
                  record.get('evidence_class') == 'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION'):
            raise ValueError('invalid reconstructed entry timing')
        # All library signals pass the entry_time >= activation_time guard above.
        # Whether the entry is observed at candle-open or candle-close affects execution
        # actionability but not forward/retrospective status — both are genuine forward
        # observations post-activation, so both are PROSPECTIVE for report purposes.
        emission_class = 'PROSPECTIVE'
        episode = entry['episode_id']
        source = 'strategy-library:'+self.instance['orchestrator_run_id']+':'+str(event_id)
        definition = self.definition
        metadata = {
            'research_only': True, 'execution_eligible': False,
            'signal_emission_class': emission_class,
            'research_evidence_class': record['evidence_class'],
            'parameter_set_id': self.identity['parameter_set_id'],
            'parameter_hash': self.identity['parameter_hash'],
            'library_run_id': self.instance['orchestrator_run_id'],
                'publication_activation_time': self.instance['publication_activation_time'],
            'library_event_id': event_id, 'library_key': self.key,
            'primitive_version': provenance.get('primitive_version'),
            'dependency_manifest_hash': provenance.get('dependency_manifest_hash'),
            'artifact_digest': provenance.get('artifact_digest'),
            'dataset_manifest_hash': provenance.get('dataset_manifest_hash'),
            'information_cutoff': iso(cutoff),
            'outcome_contract': {
                'version': 'library-research-outcome.v1',
                'timeframe_minutes': {'M5':5,'M15':15,'H1':60,'H4':240,'D1':1440}[definition.execution],
                'activation': 'SIGNAL_TIMESTAMP', 'price_basis': 'THEORETICAL_TOUCH',
                'same_candle_priority': 'AMBIGUOUS_INTRABAR',
                'time_exit_priority': 'AFTER_PRICE', 'time_exit_price': 'CLOSE',
                'max_hold_minutes': None,
            },
        }
        if self.key in EXPANSION_KEYS:
            metadata.update(strategy_version=self.strategy_version,strategy_family=definition.family,
                            sequence_primitive_version=provenance.get('sequence_primitive_version'))
        if live_open:
            metadata['entry_timing'] = provenance['entry_timing']
        return StrategySignal(
            signal_id=stable_id('SIG', {'instance': self.instance['instance_id'],
                                      'run': self.instance['orchestrator_run_id'], 'episode': episode}),
            schema_version='strategy-signal-v1', strategy_id=self.strategy_id,
            strategy_version=self.strategy_version, strategy_instance_id=self.instance['instance_id'],
            source_event_id=source, market_event_id=source, setup_id=episode,
            entry_opportunity_id=episode, economic_position_id=None,
            created_at=iso(observed), signal_timestamp=iso(entry_time),
            symbol=instrument, canonical_symbol=instrument, broker_symbol_hint=instrument+'m',
            direction=direction, entry_type='MARKET_PAPER_OBSERVATION',
            entry_price=price, stop_price=stop, target_price=target,
            risk_distance=abs(price-stop), target_distance=abs(target-price),
            target_r=abs(target-price)/abs(price-stop), timeframe=definition.execution,
            lower_timeframe=None, higher_timeframes=(definition.context,),
            entry_mechanism=(('CONTEMPORANEOUS_CANDLE_OPEN' if live_open else 'COMPLETED_CANDLE_RECONSTRUCTION'),), strategy_metadata=metadata,
            provenance={
                'source_process': 'research.strategy_library_forward',
                'classification': emission_class,
                'signal_emission_class': emission_class,
                'source_strategy_fingerprint': self.identity['parameter_hash'],
                'source_market_data_timestamp': iso(cutoff),
                'source_data_age': observed-cutoff, 'source_read_health': True,
                'orchestrator_freeze_timestamp': iso(entry_time),
                'parameter_set_id': self.identity['parameter_set_id'],
                'provider_symbol': instrument+'m', 'library_run_id': metadata['library_run_id'],
                'information_cutoff': iso(cutoff),
            }, decision_time=iso(entry_time), signal_emitted_at=iso(observed))
