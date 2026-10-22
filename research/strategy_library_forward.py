"""Cache-only forward observation of frozen library compositions.

Research state/evaluations only. No canonical entry signal, outbox, account or
broker imports. Provisioning and deployment are separate reviewed operations.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import time

from core.strategies.evaluation import canonical_hash
from core.strategies.entry_timing import MAX_ENTRY_DELAY_SECONDS
from market_data_cache.canonical import CanonicalMarketDataReader
from market_data_cache.reader import default_store
from market_data_cache.store import TIMEFRAMES
from postgres.config import PostgresConfig
from postgres.db import connect
from research.strategy_library import artifact_identity
from strategy_backtest.library import ComposedResearchRuntime
from strategy_backtest.library.forward_selection import DEFINITIONS_BY_FORWARD_SELECTOR as BY_KEY, forward_runtime
from strategy_backtest.library.runtime import STEPS, validate_series
from strategy_backtest.models import MarketEvent
from strategy_backtest.research_outcomes import resolve_research_entry

VERSION = 'library-forward-observation.v1'


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    activation_time: int
    keys: tuple[str, ...]
    symbols: dict[str, str]
    history: int = 320
    max_age_seconds: float = 330
    prospective_max_delay: float = 120
    max_retained_bars: int = 10000
    entry_mode: str = "COMPLETED_CANDLE_RECONSTRUCTION"
    entry_quote_max_age_seconds: float | None = None
    transport: str = 'POLL'

    def __post_init__(self):
        if self.transport not in {'POLL','JETSTREAM'}:
            raise ValueError('explicit supported transport required')
        if self.transport == 'JETSTREAM' and (self.entry_mode != 'CONTEMPORANEOUS_CANDLE_OPEN'
                                              or self.entry_quote_max_age_seconds is None):
            raise ValueError('JetStream requires next-open quote capture')
        if self.entry_mode not in {"COMPLETED_CANDLE_RECONSTRUCTION", "CONTEMPORANEOUS_CANDLE_OPEN"}:
            raise ValueError("explicit supported entry mode required")
        if self.entry_quote_max_age_seconds is not None:
            age = self.entry_quote_max_age_seconds
            if (self.entry_mode != 'CONTEMPORANEOUS_CANDLE_OPEN' or isinstance(age, bool)
                    or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 < age <= 15):
                raise ValueError('entry quote capture requires next-open mode and freshness in (0, 15] seconds')
        if not self.run_id or not self.keys or len(set(self.keys)) != len(self.keys):
            raise ValueError('run_id and unique strategy keys required')
        if any(key not in BY_KEY for key in self.keys):
            raise ValueError('unknown library strategy key')
        if not self.symbols or any(not k or not v for k, v in self.symbols.items()):
            raise ValueError('explicit canonical/provider mapping required')
        if len(set(self.symbols.values())) != len(self.symbols):
            raise ValueError('provider mappings must be unique')
        if type(self.activation_time) is not int or self.activation_time <= 0:
            raise ValueError('positive immutable activation epoch required')
        if type(self.history) is not int or self.history < 320:
            raise ValueError('at least 320 bootstrap bars required')
        if type(self.max_retained_bars) is not int or self.max_retained_bars < self.history:
            raise ValueError('retained history bound must cover bootstrap')
        for value in (self.max_age_seconds, self.prospective_max_delay):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('freshness windows must be positive finite seconds')

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text())
        if raw.pop('execution_eligible', None) is not False:
            raise ValueError('manifest must explicitly disable execution')
        if raw.pop('schema', None) != VERSION:
            raise ValueError('unsupported forward manifest')
        raw['keys'] = tuple(raw['keys'])
        return cls(**raw)


def readiness(config):
    return [{'key': key, 'strategy_id': BY_KEY[key].strategy_id,
             'required_timeframes': sorted({BY_KEY[key].execution, BY_KEY[key].context}),
             'cache_supported': {BY_KEY[key].execution, BY_KEY[key].context} <= set(TIMEFRAMES),
             'execution_eligible': False}
            for key in config.keys]


def _events(rows, instrument, timeframe, observed_at):
    result = []
    for row in rows:
        if row.get('complete') is False or row.get('forming') is True:
            raise ValueError('forming candle in completed cache')
        opened = int(row['time'])
        if opened + STEPS[timeframe] > observed_at:
            raise ValueError('future completed candle')
        provenance = {}
        if 'tick_volume' in row:
            provenance['tick_volume'] = row['tick_volume']
        # Arrival time belongs to the observation envelope. It must not be
        # invented as historical availability at each earlier candle close.
        result.append(MarketEvent(instrument, timeframe, opened, opened + STEPS[timeframe],
                                  float(row['open']), float(row['high']), float(row['low']),
                                  float(row['close']), source='REDIS_CANONICAL_CACHE',
                                  provenance=provenance))
    validate_series(result, timeframe, instrument, int(observed_at))
    return result


def _merge(old, fresh):
    merged = {b.open_timestamp: b for b in old}
    for bar in fresh:
        prior = merged.get(bar.open_timestamp)
        if prior is not None and asdict(prior) != asdict(bar):
            raise ValueError('conflicting completed candle revision')
        merged[bar.open_timestamp] = bar
    return sorted(merged.values(), key=lambda b: b.open_timestamp)


class ForwardObserver:
    def __init__(self, config, *, artifact=None):
        self.config = config
        self.artifact = artifact or canonical_hash({
            'strategy_artifact': artifact_identity()[0],
            'forward_adapter': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        self.binding = canonical_hash({'config': asdict(config), 'artifact': self.artifact,
                                       'adapter': VERSION})
        self.state = {'binding': self.binding, 'streams': {},
                      'manifest': {'config': asdict(config), 'artifact': self.artifact,
                                   'definitions': {key: BY_KEY[key].manifest() for key in config.keys}}}

    def restore(self, state):
        if state.get('binding') != self.binding:
            raise ValueError('run identity/configuration/artifact changed')
        self.state = deepcopy(state)

    def process(self, key, instrument, snapshot, *, observed_at, recovery=False):
        """Atomic stream transition; caller commits state and records together."""
        d = BY_KEY[key]
        if key not in self.config.keys or instrument not in self.config.symbols:
            raise ValueError('stream outside run scope')
        if not math.isfinite(observed_at) or observed_at < self.config.activation_time:
            raise ValueError('observation before activation')
        if any(getattr(snapshot, k) != v for k, v in
               [('gap_status', 'NONE'), ('recovery_status', 'NONE'),
                ('continuity_status', 'CONTINUOUS')]):
            raise ValueError('unhealthy canonical cache')
        if (not math.isfinite(snapshot.cache_age_seconds) or snapshot.cache_age_seconds < 0 or
                (not recovery and snapshot.cache_age_seconds > self.config.max_age_seconds)):
            raise ValueError('stale canonical cache')
        # The canonical bridge cache exports tick_size. Do not infer it from
        # point/digits, which describe display precision rather than tick rules.
        tick = snapshot.symbol_info.get('tick_size')
        if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick) or tick <= 0:
            raise ValueError('explicit positive canonical tick_size required')
        stream_id = key + ':' + instrument
        stored = self.state['streams'].get(stream_id)
        if stored and observed_at < stored['last_observed_at']:
            raise ValueError('observation clock moved backwards')
        streams = {} if stored is None else stored['series']
        series = {}
        for tf in sorted({d.execution, d.context}):
            fresh = _events(snapshot.bars[tf].bars, instrument, tf, observed_at)
            if not fresh:
                raise ValueError('empty canonical series')
            old = [MarketEvent(**b) for b in streams.get(tf, [])]
            required_close = int((observed_at - self.config.max_age_seconds) // STEPS[tf]) * STEPS[tf]
            if not recovery and fresh[-1].close_timestamp < required_close:
                raise ValueError('completed candle watermark is stale')
            series[tf] = _merge(old, fresh)
            if len(series[tf]) > self.config.max_retained_bars:
                raise ValueError('retained history limit reached; archive and review continuation')
        runtime = forward_runtime(key, dataset_id=self.binding, artifact_digest=self.artifact)
        if stored:
            runtime.restore_state(stored['runtime'])
        execution = series[d.execution]
        context = series[d.context]
        records = []
        previous = stored['last_close'] if stored else None
        # Bootstrap is context reconstruction only: never retain a warm-up
        # proposal as a forward opportunity or publish historical entries.
        candidates = [b for b in execution if previous is None or b.close_timestamp > previous]
        if previous is None:
            candidates = execution[-1:]
        finalized = previous
        for bar in candidates:
            # Wait for the completed context boundary covering this decision.
            # M15-first and H1-first cache delivery must finalize identically.
            boundary = bar.close_timestamp // STEPS[d.context] * STEPS[d.context]
            if context[-1].close_timestamp < boundary:
                break
            finalized = bar.close_timestamp
            prefix = [b for b in execution if b.close_timestamp <= bar.close_timestamp]
            causal_context = [b for b in context if b.close_timestamp <= bar.close_timestamp]
            outputs = runtime.advance(prefix, causal_context, tick_size=tick)
            if previous is None or bar.close_timestamp <= self.config.activation_time:
                checkpoint = runtime.snapshot_state()
                checkpoint['state']['pending'].clear()
                checkpoint['state']['entries'].clear()
                checkpoint['state'].get('breakout_episodes', {}).clear()
                runtime.restore_state(checkpoint)
                continue
            for evaluation in outputs:
                payload = evaluation.to_dict()
                evidence = 'PROSPECTIVE_SETUP_OBSERVATION'
                if observed_at - bar.close_timestamp > self.config.prospective_max_delay:
                    evidence = 'RECOVERY_RESEARCH_EVIDENCE'
                if evaluation.provenance.get('research_entry'):
                    evidence = 'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION'
                identity = canonical_hash({'binding': self.binding, 'stream': stream_id,
                                           'evaluation': payload})
                records.append({'event_id': identity, 'kind': 'EVALUATION', 'strategy_key': key,
                                'instrument': instrument, 'observed_at': observed_at,
                                'decision_time': bar.close_timestamp, 'evidence_class': evidence,
                                'processing_delay_seconds': observed_at - bar.close_timestamp,
                                'execution_eligible': False, 'payload': payload})
        if self.config.entry_mode == 'CONTEMPORANEOUS_CANDLE_OPEN' and previous is not None and not recovery:
            from market_data_cache.opening import read_opening
            raw = {'opening_observations': getattr(snapshot, 'opening_observations', {})}
            opening = read_opening(raw, instrument, self.config.symbols[instrument], d.execution,
                                   observed_at=observed_at)
            pending = runtime.snapshot_state()['state']['pending'].get(instrument)
            if opening is not None and pending is not None:
                if opening.open_time == pending['intended_entry_time'] and 0 <= observed_at - opening.open_time <= MAX_ENTRY_DELAY_SECONDS:
                    quote = None
                    if self.config.entry_quote_max_age_seconds is not None:
                        from core.strategies.entry_quote import capture_entry_quote
                        quote = capture_entry_quote(getattr(snapshot, 'entry_quote', None),
                            instrument=instrument, provider_symbol=self.config.symbols[instrument],
                            direction=pending['direction'], entry_time=opening.open_time,
                            observed_at=observed_at, max_age_seconds=self.config.entry_quote_max_age_seconds)
                    for evaluation in runtime.observe_open(opening, setup_available_at=observed_at, now=observed_at):
                        payload = evaluation.to_dict()
                        if quote is not None:
                            # Quote is an observed execution reference. The frozen theoretical
                            # open-price geometry and resolver basis remain explicit and distinct.
                            payload['provenance']['entry_quote'] = quote
                        identity = canonical_hash({'binding': self.binding, 'stream': stream_id, 'evaluation': payload})
                        records.append({'event_id': identity, 'kind': 'EVALUATION', 'strategy_key': key,
                                        'instrument': instrument, 'observed_at': observed_at,
                                        'decision_time': opening.open_time,
                                        'evidence_class': 'CONTEMPORANEOUS_CANDLE_OPEN',
                                        'processing_delay_seconds': observed_at-opening.open_time,
                                        'execution_eligible': False, 'payload': payload})
        # Use the existing pure research resolver. Never write canonical or
        # broker outcome tables; reference outcomes remain theoretical for both entry modes.
        for entry in runtime.accepted_entries():
            outcome = resolve_research_entry(entry, execution,
                                            timeframe_seconds=STEPS[d.execution]).to_dict()
            identity = canonical_hash({'binding': self.binding, 'entry': entry.episode_id,
                                       'outcome': outcome})
            records.append({'event_id': identity, 'kind': 'THEORETICAL_OUTCOME', 'strategy_key': key,
                            'instrument': instrument, 'observed_at': observed_at,
                            'decision_time': entry.entry_timestamp,
                            'evidence_class': 'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION',
                            'execution_eligible': False, 'payload': outcome})
        if finalized is None:
            raise ValueError('bootstrap waiting for completed context watermark')
        updated = {'last_close': finalized, 'last_observed_at': observed_at,
                   'series': {tf: [asdict(b) for b in bars] for tf, bars in series.items()},
                   'runtime': runtime.snapshot_state()}
        self.state['streams'][stream_id] = updated
        return records


class PostgresResearchLedger:
    """One transaction and per-run advisory lock cover checkpoint + append-only events."""
    def __init__(self, conn, run_id):
        self.conn, self.run_id = conn, run_id

    def lock_and_load(self):
        with self.conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                        ('strategy-library-forward:' + self.run_id,))
            cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s',
                        (self.run_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def save(self, state, records):
        with self.conn.cursor() as cur:
            for record in records:
                cur.execute('''INSERT INTO research.strategy_library_forward_event
                    (run_id,event_id,kind,strategy_key,instrument,observed_at,payload)
                    VALUES (%s,%s,%s,%s,%s,to_timestamp(%s),%s::jsonb)
                    ON CONFLICT (run_id,event_id) DO NOTHING''',
                            (self.run_id, record['event_id'], record['kind'], record['strategy_key'],
                             record['instrument'], record['observed_at'], dumps(record)))
            cur.execute('''INSERT INTO research.strategy_library_forward_state (run_id,state)
                VALUES (%s,%s::jsonb) ON CONFLICT (run_id) DO UPDATE
                SET state=EXCLUDED.state, updated_at=now()''', (self.run_id, dumps(state)))


def run_once(config):
    if config.transport != 'POLL':
        raise ValueError('JetStream manifests require the event consumer, not the polling runner')
    cycle_started_at = time.time()
    stages = {'cycle_started_at': cycle_started_at}
    observer = ForwardObserver(config)
    reader = CanonicalMarketDataReader(default_store(), max_age=config.max_age_seconds)
    pg = PostgresConfig.from_env()
    pg.require_explicit_target()
    statuses, records = [], []
    with connect(pg) as conn:
        ledger = PostgresResearchLedger(conn, config.run_id)
        stored = ledger.lock_and_load()
        stages['lock_acquired_at'] = time.time()
        if stored:
            observer.restore(stored)
        snapshots = {}
        for ready in readiness(config):
            key = ready['key']
            if not ready['cache_supported']:
                statuses.append({'key': key, 'status': 'BLOCKED_UNSUPPORTED_TIMEFRAME'})
                continue
            for instrument, provider in config.symbols.items():
                d = BY_KEY[key]
                if d.symbols and instrument not in d.symbols:
                    statuses.append({'key': key, 'instrument': instrument, 'status': 'OUTSIDE_STRATEGY_SCOPE'})
                    continue
                try:
                    cache_key = (instrument, tuple(ready['required_timeframes']))
                    if cache_key not in snapshots:
                        read_started = time.time()
                        kwargs = {'include_entry_quote': True} if config.entry_quote_max_age_seconds is not None else {}
                        view = reader.get_snapshot(instrument, provider,
                            {tf: config.history for tf in ready['required_timeframes']}, **kwargs)
                        snapshots[cache_key] = (view, read_started, time.time())
                    view, read_started, read_completed = snapshots[cache_key]
                    evaluation_started = time.time()
                    emitted = observer.process(key, instrument, view, observed_at=evaluation_started)
                    evaluation_completed = time.time()
                    for event in emitted:
                        event['stage_timing'] = {**stages, **getattr(view, 'source_timing', {}),
                            'snapshot_read_started_at': read_started, 'snapshot_read_completed_at': read_completed,
                            'evaluation_started_at': evaluation_started, 'evaluation_completed_at': evaluation_completed}
                    records.extend(emitted)
                    latest_close = max(int(row['time']) + STEPS[d.execution]
                                       for row in view.bars[d.execution].bars)
                    last_close = observer.state['streams'][key + ':' + instrument]['last_close']
                    statuses.append({'key': key, 'instrument': instrument,
                                     'status': 'OK' if last_close == latest_close else 'WAITING_CONTEXT',
                                     'records': len(emitted),
                                     'last_finalized_close': observer.state['streams'][key + ':' + instrument]['last_close']})
                except (ValueError, KeyError, RuntimeError) as exc:
                    statuses.append({'key': key, 'instrument': instrument, 'status': 'BLOCKED',
                                     'error_type': type(exc).__name__,
                                     'error_message': str(exc)})
        observer.state['health'] = {
            'observed_at': time.time(), 'streams': statuses,
            'status': 'OK' if statuses and any(s['status']=='OK' for s in statuses)
                      and all(s['status'] in {'OK', 'OUTSIDE_STRATEGY_SCOPE'} for s in statuses)
                      else 'DEGRADED'}
        stages['ledger_write_started_at'] = time.time()
        ledger.save(observer.state, records)
        if records:
            from infrastructure.messaging.runtime_events import notify_ready
            notify_ready([record['event_id'] for record in records], conn=conn)
        stages['ledger_write_completed_at'] = time.time()
    stages['transaction_committed_at'] = time.time()
    print(dumps({'event': 'library_observer_batch_committed', 'run_id': config.run_id,
                 'stage_timing': stages, 'event_ids': [r['event_id'] for r in records]}), flush=True)
    return {'schema': VERSION, 'run_id': config.run_id, 'binding': observer.binding,
            'execution_eligible': False, 'bridge_requests': 0, 'records': len(records), 'streams': statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['readiness', 'health', 'once', 'run'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args()
    config = RunConfig.load(args.manifest)
    if args.command == 'readiness':
        print(dumps(readiness(config)))
        return
    if args.command == 'health':
        with connect(PostgresConfig.from_env()) as conn:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s',
                            (config.run_id,))
                row = cur.fetchone()
                health = row[0].get('health', {}) if row else {}
                valid = bool(row and row[0].get('binding') == ForwardObserver(config).binding
                             and health.get('status') == 'OK'
                             and 0 <= time.time()-health.get('observed_at', 0) <= config.max_age_seconds)
                print(dumps({'healthy': valid, 'run_id': config.run_id}))
                raise SystemExit(0 if valid else 1)
    if not math.isfinite(args.interval) or args.interval < 1:
        raise ValueError('interval must be at least one second')
    from infrastructure.messaging.runtime_events import Wakeup
    wakeup = Wakeup(symbols=config.symbols.values()).start()
    try:
        while True:
            cycle_started = time.monotonic()
            print(dumps(run_once(config)), flush=True)
            if args.command == 'once':
                break
            # Evaluation/persistence time counts toward the cadence; cycles never overlap.
            wakeup.wait(max(0.0, args.interval-(time.monotonic()-cycle_started)))
    finally:
        wakeup.close()


if __name__ == '__main__':
    main()
