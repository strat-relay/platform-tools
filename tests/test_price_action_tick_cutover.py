from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.price_action_tick_cutover import SOURCES, MODE, prepare
from research.price_action_forward_observer import Observer
from research.price_action_metadata import POLICY, validate_checkpoint
from research.price_action_next_open import mode_binding
from strategy_backtest.models import fingerprint

IMAGE = 'ghcr.io/strat-relay/trading-platform-runtime@sha256:' + 'a' * 64


def source_documents():
    return [{'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': name, 'namespace': 'trading', 'labels': {'app.kubernetes.io/name': name}},
        'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                 'selector': {'matchLabels': {'app.kubernetes.io/name': name}},
                 'template': {'metadata': {'labels': {'app.kubernetes.io/name': name}},
                    'spec': {'containers': [{'name': 'observer', 'image': IMAGE,
                       'envFrom': [{'secretRef': {'name': 'old-db'}}],
                       'env': [{'name': 'PRICE_ACTION_INSTANCE_ID', 'value': old},
                               {'name': 'PRICE_ACTION_STRATEGY', 'value': key},
                               {'name': 'PRICE_ACTION_MCP_URL', 'value': 'old-bridge'},
                               {'name': 'REDIS_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'redis', 'key': 'password'}}},
                               {'name': 'MARKET_DATA_REDIS_URL', 'value': 'redis://cache'}]}]}}}}
        for key, (old, _, _, name) in SOURCES.items()]


def bundle(documents=None, **overrides):
    args = dict(run_id='pa-test-20261019', activation_time=1792412100,
                image=IMAGE, db_secret='verified-pa-research-db')
    args.update(overrides)
    return prepare(source_documents() if documents is None else documents, **args)


class PriceActionTickCutoverTests(unittest.TestCase):
    def test_both_new_runs_bootstrap_and_bind_without_touching_original_input(self):
        source = source_documents(); before = deepcopy(source)
        result = bundle(source)
        self.assertEqual(source, before)
        self.assertEqual(result, bundle(source))
        resources = result['workload.json']['items']
        manifests = resources[0]['data']
        self.assertTrue(resources[0]['immutable'])
        self.assertEqual(len([r for r in resources if r['kind'] == 'NetworkPolicy']), 2)
        for key, info in result['run.json']['instances'].items():
            self.assertNotEqual(info['instance_id'], info['original_instance_id'])
            manifest = json.loads(manifests[key + '.json'])
            self.assertEqual(manifest['market_input_policy'], POLICY)
            self.assertEqual(manifest['entry_mode'], MODE)
            digest = manifest.pop('manifest_hash')
            self.assertEqual(fingerprint(manifest), digest)
            manifest['manifest_hash'] = digest
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'run.json'; path.write_text(json.dumps(manifest))
                observer = Observer(Path(directory) / 'state', path, strategy_filter=key)
                validate_checkpoint(observer.state, binding=observer.input_binding)
                self.assertIn(observer.input_binding, result['activate.sql'])
                self.assertIn(mode_binding(path, info['instance_id'], key), result['activate.sql'])
            deployment = next(r for r in resources if r['kind'] == 'Deployment' and r['metadata']['name'] == info['workload'])
            c = deployment['spec']['template']['spec']['containers'][0]
            env = {e['name']: e.get('value') for e in c['env']}
            self.assertEqual(env['PRICE_ACTION_INSTANCE_ID'], info['instance_id'])
            self.assertEqual(env['PRICE_ACTION_POLL_SECONDS'], '10')
            self.assertEqual(env['EXECUTION_AUTHORITY_MODE'], 'DISABLED')
            self.assertNotIn('PRICE_ACTION_MCP_URL', env)
            self.assertEqual(c['envFrom'], [{'secretRef': {'name': 'verified-pa-research-db'}}])
            self.assertIn(info['instance_id'], result['register.sql'])
        self.assertTrue(all(d['spec']['replicas'] == 0 for d in result['legacy-observers.json']['items']))

    def test_sql_preserves_evidence_and_requires_fresh_bound_checkpoints(self):
        result = bundle()
        for name in ('register.sql', 'activate.sql', 'quiesce.sql'):
            sql = result[name]
            self.assertTrue(sql.startswith('BEGIN;')); self.assertTrue(sql.endswith('COMMIT;\n'))
            for forbidden in ('DELETE ', 'TRUNCATE ', 'UPDATE research.', 'UPDATE strategy.entry', 'GRANT '):
                self.assertNotIn(forbidden, sql)
        self.assertIn('n.parameter_set_id=o.parameter_set_id', result['register.sql'])
        self.assertIn("false,false,'OFF'", result['register.sql'])
        self.assertIn('last_observed_at >', result['activate.sql'])
        self.assertIn("successful_instrument_count')::int=3", result['activate.sql'])
        self.assertNotIn('online=true', result['register.sql'])
        self.assertNotIn('execution_eligible=true', result['activate.sql'])

    def test_rejects_misbound_deployments_and_unpinned_images(self):
        for values in ({'image': 'runtime:latest'}, {'activation_time': True},
                       {'activation_time': 1}, {'run_id': '../legacy'}, {'db_secret': ''}):
            with self.subTest(values=values), self.assertRaises(ValueError): bundle(**values)
        docs = source_documents(); docs[0]['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'other'
        with self.assertRaises(ValueError): bundle(docs)
        with self.assertRaises(ValueError): bundle(source_documents()[:1])

    def test_both_original_ids_still_fail_closed_before_any_io(self):
        from research.price_action_forward_runtime import run_once
        for key, (instance, _, _, _) in SOURCES.items():
            with patch.dict('os.environ', {'PRICE_ACTION_INSTANCE_ID': instance,
                'PRICE_ACTION_STRATEGY': key,
                'PRICE_ACTION_SYMBOL_MAP': '{"BTCUSD":"BTCUSDm"}'}):
                with self.assertRaisesRegex(ValueError, 'require a new instance'):
                    run_once()
