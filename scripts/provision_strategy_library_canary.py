"""Operator-controlled canary provisioning via kubectl; credentials never printed.

Run on the operator Mac. Requires cluster admin exec/Secret permissions and a
PostgreSQL container whose local POSTGRES_USER can provision roles. The observer
must remain scaled to zero until this succeeds and GitOps activation is reviewed.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys

SECRET = 'strategy-library-observer-db'
LOGIN = 'strategy_library_forward_canary'
OWNER = 'strategy-library-canary-v1'
MIGRATION = '063_strategy_library_forward_canary.sql'


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def migration_transaction(sql, checksum):
    """Match the platform runner lock and ledger/checksum contract exactly."""
    if '$library_migration$' in sql:
        raise ValueError('unexpected delimiter in migration')
    return '''BEGIN;
SET LOCAL statement_timeout = '60s';
SELECT pg_advisory_xact_lock(hashtext('mt5-native-bridge-schema-migrations'));
DO $library_migration$ BEGIN
IF EXISTS (SELECT 1 FROM platform.schema_migrations
           WHERE version ~ '^[0-9]+$' AND version::numeric = 63
             AND (version <> '063' OR filename <> ''' + quote(MIGRATION) + '''
                  OR checksum_sha256 <> ''' + quote(checksum) + ''')) THEN
    RAISE EXCEPTION 'Migration 063 already occupied or checksum mismatch';
END IF;
IF NOT EXISTS (SELECT 1 FROM platform.schema_migrations WHERE version='063') THEN
''' + sql + '''
INSERT INTO platform.schema_migrations(version,filename,checksum_sha256)
VALUES ('063', ''' + quote(MIGRATION) + ', ' + quote(checksum) + ''');
END IF;
END $library_migration$;
COMMIT;
'''


def login_sql(password):
    marker = OWNER + ':dedicated-login'
    return '''BEGIN;
DO $login$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''' + quote(LOGIN) + ''') THEN
    CREATE ROLE ''' + LOGIN + ' LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD ' + quote(password) + ''';
    COMMENT ON ROLE ''' + LOGIN + ' IS ' + quote(marker) + ''';
ELSIF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=''' + quote(LOGIN) + '''
    AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
    AND NOT rolreplication AND NOT rolbypassrls
    AND shobj_description(oid, 'pg_authid')=''' + quote(marker) + ''') THEN
    RAISE EXCEPTION 'Existing canary login has incompatible identity or privileges';
END IF;
END $login$;
GRANT strategy_library_observer TO ''' + LOGIN + ''';
COMMIT;
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--sql', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    logs, sensitive = [], []

    def run(arguments, payload=None, *, allow_missing=False):
        result = subprocess.run(['kubectl', '--kubeconfig', args.kubeconfig,
                                 '--request-timeout=30s', *arguments],
                                input=payload, text=True, capture_output=True, timeout=120)
        if result.returncode and not allow_missing:
            raise RuntimeError(result.stderr or result.stdout or 'kubectl failed')
        return result

    try:
        context = run(['config','current-context']).stdout.strip()
        if context != 'local':
            raise RuntimeError('Expected the reviewed local context; refusing a different context')
        logs.append('Context: '+context+'; namespace: trading')
        deployment = run(['-n','trading','get','deployment','strategy-library-canary-v1-observer',
                          '--ignore-not-found','-o','json']).stdout.strip()
        if deployment and json.loads(deployment)['spec'].get('replicas', 1) != 0:
            raise RuntimeError('Canary must be absent or scaled to zero before provisioning')
        info_code = '''import json
from postgres.db import connect
conn=connect()
with conn.cursor() as cur:
 cur.execute("SELECT 'strategy.entry_signals'::regclass::oid")
 oid=cur.fetchone()[0]
print(json.dumps({'PGHOST':conn.info.host,'PGPORT':str(conn.info.port),'PGDATABASE':conn.info.dbname,'canonical_signal_oid':oid}))
conn.close()
'''
        metadata = json.loads(run(['-n','trading','exec','-i','deployment/unified-outcome-resolver',
                                 '-c','resolver','--','python3','-'], info_code).stdout)
        target = {k:metadata[k] for k in ('PGHOST','PGPORT','PGDATABASE')}
        if not all(target.get(k) for k in ('PGHOST','PGPORT','PGDATABASE')):
            raise RuntimeError('Explicit DB target unavailable')
        logs.append('Database target verified from existing resolver connection')
        existing = run(['-n','trading','get','secret',SECRET,'--ignore-not-found','-o','json']).stdout.strip()
        if existing:
            document = json.loads(existing)
            if document.get('metadata',{}).get('labels',{}).get('stratrelay.app/owner') != OWNER:
                raise RuntimeError('Secret name collision; existing Secret is not managed by this canary')
            credentials = {k:base64.b64decode(v).decode() for k,v in document['data'].items()}
            sensitive.extend([credentials.get('PGPASSWORD',''), document['data'].get('PGPASSWORD','')])
            if any(credentials.get(k) != v for k,v in target.items()) or credentials.get('PGUSER') != LOGIN:
                raise RuntimeError('Existing observer Secret target mismatch')
            password = credentials['PGPASSWORD']
            logs.append('Reusing existing canary-owned Secret; password is not rotated')
        else:
            password = secrets.token_urlsafe(36)
            sensitive.extend([password,base64.b64encode(password.encode()).decode()])
            document = {'apiVersion':'v1','kind':'Secret','type':'Opaque',
                        'metadata':{'name':SECRET,'namespace':'trading',
                                    'labels':{'stratrelay.app/owner':OWNER}},
                        'stringData':{**target,'PGUSER':LOGIN,'PGPASSWORD':password}}
            run(['create','-f','-'], json.dumps(document))
            logs.append('Created dedicated credential Secret; values withheld')
        sql = Path(args.sql).read_text()
        checksum = hashlib.sha256(Path(args.sql).read_bytes()).hexdigest()
        admin = ['-n','trading','exec','-i','trading-postgres-0','--','sh','-c',
                 'exec psql -X -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1"',
                 'sh', target['PGDATABASE']]
        run(admin, migration_transaction(sql, checksum))
        logs.append('Applied/verified migration 063 under platform migration lock; SHA256: '+checksum)
        run(admin, login_sql(password))
        logs.append('Created/verified dedicated non-admin login with observer-group membership')
        # Credential testing occurs inside an existing pod; no password goes to
        # argv, output, Git, or the operator report. The session is read-only.
        verify = '''import json
import psycopg
c=psycopg.connect(host={host},port={port},dbname={db},user={user},password={password})
with c.cursor() as cur:
 cur.execute("SET TRANSACTION READ ONLY")
 cur.execute("SELECT has_table_privilege(current_user,'research.strategy_library_forward_state','INSERT'),has_table_privilege(current_user,'research.strategy_library_forward_event','INSERT'),has_table_privilege(current_user,{canonical_oid}::oid,'INSERT')")
 privileges=cur.fetchone()
 if privileges != (True,True,False): raise RuntimeError('Observer privilege boundary failed')
 cur.execute("SELECT count(*) FROM research.strategy_library_forward_state")
 print('Dedicated credential authentication and research-only privilege checks passed')
c.rollback()
c.close()
'''.format(host=repr(target['PGHOST']),port=repr(target['PGPORT']),db=repr(target['PGDATABASE']),
           user=repr(LOGIN),password=repr(password),canonical_oid=int(metadata['canonical_signal_oid']))
        run(['-n','trading','exec','-i','deployment/unified-outcome-resolver','-c','resolver',
             '--','python3','-'], verify)
        logs.append('Dedicated authentication and privilege verification passed')
        logs.append('PROVISIONED. Observer remains stopped; activate only through GitOps.')
        exit_code = 0
    except Exception as exc:
        logs.append('STOPPED: '+str(exc))
        logs.append('No activation attempted. Preserve created Secret for retry; do not share its contents.')
        exit_code = 1
    output = '\n'.join(logs)+'\n'
    for value in sensitive:
        if value: output = output.replace(value, '[REDACTED]')
    Path(args.report).write_text(output)
    print(output, end='')
    raise SystemExit(exit_code)


if __name__ == '__main__': main()
