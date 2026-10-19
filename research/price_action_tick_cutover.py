"""Prepare both corrected PA runs without touching a database or cluster.

The registration SQL resolves each original instance's exact frozen version and
parameter set in PostgreSQL. No legacy checkpoint, event or outcome is updated.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5

from research.price_action_forward_observer import freeze, identities, source_hash
from research.price_action_next_open import mode_binding
from research.price_action_metadata import POLICY
from strategy_backtest.models import fingerprint

SOURCES = {
    'sr_retest': ('7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104',
                  'price_action_sr_retest_v1', 'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST',
                  'price-action-sr-retest-v1-observer'),
    'engulfing_continuation': ('7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204',
                  'price_action_engulfing_continuation_v1', 'PRICE_ACTION_ENGULFING_CONTINUATION',
                  'price-action-engulfing-continuation-v1-observer'),
}
SYMBOLS = {'XAUUSD': 'XAUUSDm', 'BTCUSD': 'BTCUSDm', 'USDJPY': 'USDJPYm'}
NAMESPACE = UUID('fe8fb369-9215-4937-ade8-ff52e864601a')
MODE = 'CONTEMPORANEOUS_CANDLE_OPEN'


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def js(value):
    return quote(json.dumps(value, sort_keys=True)) + '::jsonb'


def gate(condition, message):
    return 'DO $pa$ BEGIN IF NOT (' + condition + ') THEN RAISE EXCEPTION ' + quote(message) + '; END IF; END $pa$;'


def prepare(documents, *, run_id, activation_time, image, db_secret):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', run_id):
        raise ValueError('unique DNS-safe run ID, at most 40 characters, required')
    if type(activation_time) is not int or activation_time <= 0 or activation_time % 900:
        raise ValueError('explicit UTC M15-boundary activation epoch required')
    if not re.fullmatch(r'ghcr\.io/strat-relay/trading-platform-runtime@sha256:[0-9a-f]{64}', image):
        raise ValueError('verified immutable main-built runtime image required')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', db_secret):
        raise ValueError('verified research-only PostgreSQL Secret name required')
    legacy = deepcopy(documents)
    deployments = {d['metadata']['name']: d for d in legacy if d['kind'] == 'Deployment'}
    if set(deployments) != {v[3] for v in SOURCES.values()}:
        raise ValueError('expected exactly the two original PA deployments')
    run = {'run_id': run_id, 'image': image, 'activation_time': activation_time,
           'market_input_policy': POLICY, 'entry_mode': MODE, 'instances': {}}
    registration = ["BEGIN; SET LOCAL statement_timeout='15s'; SELECT pg_advisory_xact_lock(hashtext('pa-tick-cutover')); "]
    activation = [registration[0]]
    quiesce = ['BEGIN;']
    resources = []
    config_name = run_id + '-manifests'
    data = {}
    expected = identities()
    for key, (original, evaluator, strategy, old_name) in SOURCES.items():
        instance = str(uuid5(NAMESPACE, run_id + ':' + key))
        name = 'pa-tick-' + ('sr' if key == 'sr_retest' else 'engulf') + '-' + fingerprint(run_id)[:10]
        source = deployments[old_name]
        container = source['spec']['template']['spec']['containers'][0]
        old_env = {e['name']: e.get('value') for e in container.get('env', [])}
        if old_env.get('PRICE_ACTION_INSTANCE_ID') != original or old_env.get('PRICE_ACTION_STRATEGY') != key:
            raise ValueError('original deployment identity mismatch')
        source['spec']['replicas'] = 0
        t0 = datetime.fromtimestamp(activation_time, timezone.utc).isoformat().replace('+00:00', 'Z')
        with TemporaryDirectory() as directory:
            manifest = freeze(t0=t0, output=Path(directory) / 'manifest.json')
        manifest.pop('manifest_hash')
        manifest.update(run_id=run_id, instance_id=instance, entry_mode=MODE,
                        universe_requested=list(SYMBOLS), instrument_mapping=SYMBOLS,
                        timestamp_semantics='UTC candle-open; completed-candle confirmation; contemporaneous opening evidence only')
        manifest['manifest_hash'] = fingerprint(manifest)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(manifest))
            timing_binding = mode_binding(path, instance, key)
        input_binding = fingerprint({'manifest': manifest, 'source_hash': source_hash()})
        data[key + '.json'] = json.dumps(manifest, indent=2, sort_keys=True)
        attrs = {'status': 'RESEARCH', 'research_only': True, 'mode': 'OBSERVER_ONLY',
                 'market_input_policy': POLICY, 'broker_writes': False, 'execution_eligible': False,
                 'signal_publication': False, 'publication_mode': 'ORCHESTRATOR_RESEARCH_ONLY',
                 'publication_activation_time': activation_time, 'corrected_run_id': run_id,
                 'supersedes_instance_id': original, 'forward_image': image,
                 'forward_entry_mode': MODE, 'manifest_hash': manifest['manifest_hash']}
        identity = ("SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i "
                    "JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id "
                    "JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id "
                    "WHERE i.id=" + quote(original) + "::uuid AND v.evaluator_key=" + quote(evaluator) +
                    " AND v.version_label='V1' AND v.lifecycle='FROZEN' AND p.frozen AND "
                    "p.strategy_version_id=v.id AND p.fingerprint=" + quote(expected[key]['parameter_hash']) +
                    " AND p.values=" + js(expected[key]['parameters']))
        registration.append(gate('EXISTS (' + identity + ')', 'original frozen PA binding mismatch: ' + key))
        registration.append("INSERT INTO strategy_mgmt.strategy_instance_v2 "
            "(id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,execution_mode,instruments,attributes,created_by) "
            "SELECT " + quote(instance) + "::uuid,i.strategy_version_id,i.parameter_set_id," + quote(key + ' corrected ticks ' + run_id) +
            ",false,false,'OFF'," + js(list(SYMBOLS)) + "," + js(attrs) +
            " || jsonb_build_object('parameter_set_id',p.parameter_set_id)" +
            ",'pa-tick-cutover' FROM strategy_mgmt.strategy_instance_v2 i "
            "JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id WHERE i.id=" + quote(original) +
            "::uuid ON CONFLICT (id) DO NOTHING;")
        # Assert full immutable identity even on idempotent registration attempts.
        binding = ("EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 n "
            "JOIN strategy_mgmt.strategy_instance_v2 o ON o.id=" + quote(original) + "::uuid "
            "JOIN strategy_mgmt.parameter_set p ON p.id=n.parameter_set_id "
            "WHERE n.id=" + quote(instance) + "::uuid AND n.strategy_version_id=o.strategy_version_id "
            "AND n.parameter_set_id=o.parameter_set_id AND n.attributes @> " + js({k: v for k, v in attrs.items() if k != 'signal_publication'}) +
            " AND n.attributes->>'parameter_set_id'=p.parameter_set_id AND n.instruments=" + js(list(SYMBOLS)) +
            " AND NOT n.execution_eligible AND n.execution_mode='OFF')")
        registration.append(gate(binding, 'corrected PA identity conflict: ' + key))
        for symbol in SYMBOLS:
            registration.append("INSERT INTO strategy.instrument_membership "
                "(strategy_instance_id,strategy_id,canonical_instrument,state,updated_by) VALUES (" +
                ','.join(map(quote, (instance, strategy, symbol, 'ACTIVE', 'pa-tick-cutover'))) + ") ON CONFLICT DO NOTHING;")
        activation.append(gate(binding, 'corrected PA identity changed: ' + key))
        activation.append(gate("EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id=" + quote(strategy) +
                               " AND enabled)", 'PA parent must already be active: ' + key))
        activation.append(gate("EXISTS (SELECT 1 FROM research.price_action_observer_state WHERE instance_id=" + quote(instance) +
            " AND strategy_key=" + quote(key) + " AND state->>'market_input_policy'=" + quote(POLICY) +
            " AND state->>'t0'=" + quote(t0) + " AND state->>'entry_timing_mode'=" + quote(MODE) +
            " AND state->>'market_input_binding'=" + quote(input_binding) +
            " AND state->>'entry_timing_binding'=" + quote(timing_binding) +
            " AND state->>'status'='RUNNING' AND (state->'runtime'->>'successful_instrument_count')::int=3 "
            "AND state->'instrument_tick_sizes' ?& ARRAY['XAUUSD','BTCUSD','USDJPY'] "
            "AND last_observed_at > now()-interval '120 seconds' AND last_observed_at <= now())",
            'fresh corrected checkpoint required: ' + key))
        activation.append(gate("(SELECT count(*)=3 AND bool_and(canonical_instrument=ANY(ARRAY['XAUUSD','BTCUSD','USDJPY'])) "
            "FROM strategy.instrument_membership WHERE strategy_instance_id=" + quote(instance) +
            " AND strategy_id=" + quote(strategy) + " AND state='ACTIVE')", 'corrected PA membership mismatch'))
        activation.append("UPDATE strategy_mgmt.strategy_instance_v2 SET online=false,updated_at=now() WHERE id=" + quote(original) + "::uuid;")
        activation.append("UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,attributes=attributes || "
                          "'{\"signal_publication\":true}'::jsonb,updated_at=now() WHERE id=" + quote(instance) + "::uuid;")
        quiesce.append("UPDATE strategy_mgmt.strategy_instance_v2 SET online=false,updated_at=now() WHERE id=" + quote(instance) +
                      "::uuid AND attributes->>'corrected_run_id'=" + quote(run_id) + ";")
        deployment = deepcopy(source)
        deployment['metadata']['name'] = name
        labels = deployment['metadata']['labels']
        labels['app.kubernetes.io/name'] = name
        deployment['spec']['selector']['matchLabels'] = {'app.kubernetes.io/name': name}
        deployment['spec']['replicas'] = 1
        pod = deployment['spec']['template']
        pod['metadata']['labels'] = deepcopy(labels)
        spec = pod['spec']; c = spec['containers'][0]
        c['image'] = image
        c['envFrom'] = [{'secretRef': {'name': db_secret}}]
        values = {'PRICE_ACTION_INSTANCE_ID': instance, 'PRICE_ACTION_STRATEGY': key,
                  'PRICE_ACTION_MANIFEST_PATH': '/config/' + key + '.json',
                  'PRICE_ACTION_ENTRY_MODE': MODE, 'PRICE_ACTION_POLL_SECONDS': '10',
                  'PRICE_ACTION_SYMBOL_MAP': json.dumps(SYMBOLS), 'EXECUTION_AUTHORITY_MODE': 'DISABLED',
                  'SIGNAL_AUTHORITY_MODE': 'RESEARCH_ONLY', 'ORCHESTRATOR_MODE': 'false'}
        c['env'] = [e for e in c['env'] if e['name'] in ('REDIS_PASSWORD', 'MARKET_DATA_REDIS_URL')]
        c['env'].extend({'name': k, 'value': v} for k, v in values.items())
        c.setdefault('volumeMounts', []).append({'name': 'manifest', 'mountPath': '/config', 'readOnly': True})
        spec.setdefault('volumes', []).append({'name': 'manifest', 'configMap': {'name': config_name}})
        resources.append(deployment)
        resources.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': name, 'namespace': 'trading'},
            'spec': {'podSelector': {'matchLabels': {'app.kubernetes.io/name': name}},
                     'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': [
                         {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'trading'}},
                                  'podSelector': {'matchLabels': {'app.kubernetes.io/name': app}}}],
                          'ports': [{'port': port, 'protocol': 'TCP'}]}
                         for app, port in [('trading-postgres', 5432), ('trading-redis', 6379)]] + [
                         {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}}],
                          'ports': [{'port': 53, 'protocol': p} for p in ('UDP', 'TCP')]}]}})
        run['instances'][key] = {'original_instance_id': original, 'instance_id': instance,
                                 'workload': name, 'manifest_hash': manifest['manifest_hash']}
    resources.insert(0, {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
                         'metadata': {'name': config_name, 'namespace': 'trading'}, 'data': data})
    return {'run.json': run, 'workload.json': {'apiVersion': 'v1', 'kind': 'List', 'items': resources},
            'legacy-observers.json': {'apiVersion': 'v1', 'kind': 'List', 'items': legacy},
            'register.sql': '\n'.join(registration + ['COMMIT;']) + '\n',
            'activate.sql': '\n'.join(activation + ['COMMIT;']) + '\n',
            'quiesce.sql': '\n'.join(quiesce + ['COMMIT;']) + '\n'}


def main():
    import argparse
    import time
    p = argparse.ArgumentParser(__doc__)
    for arg in ('gitops-source', 'run-id', 'image', 'db-secret', 'output'):
        p.add_argument('--' + arg, required=True)
    p.add_argument('--activation-time', required=True, type=int)
    a = p.parse_args()
    if a.activation_time <= time.time():
        p.error('choose a future activation boundary; do not backdate a corrected run')
    # kubectl's JSON output avoids adding a YAML dependency to runtime images.
    source = json.loads(Path(a.gitops_source).read_text())
    documents = source['items'] if isinstance(source, dict) and source.get('kind') == 'List' else source
    bundle = prepare(documents,
                     run_id=a.run_id, activation_time=a.activation_time, image=a.image, db_secret=a.db_secret)
    output = Path(a.output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        p.error('output directory must be empty; never overwrite a frozen cutover bundle')
    for name, value in bundle.items():
        (output / name).write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + '\n')
    print(json.dumps(bundle['run.json'], indent=2))


if __name__ == '__main__':
    main()
