"""One-time, checksum-pinned admin application of migration 065; no workload changes."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess

FILENAME = '065_strategy_library_reader_permissions.sql'
CHECKSUM = 'a8a63151ee915be84e1a01b36ae1a852e2a79c3bda0f78068e39cbf1200a9e2e'


def safe_error(message):
    message = re.sub(r'(?i)postgres(?:ql)?://[^\s]+', '<database-uri-redacted>', message)
    message = re.sub(r'(?i)\bBearer\s+[^\s]+', 'Bearer <redacted>', message)
    message = re.sub(r'''(?ix)(\b(?:password|token|authorization|client-key-data|client-certificate-data|certificate-authority-data)\s*[:=]\s*)(?:'[^']*'|"[^"]*"|[^\s,;]+)''',
                     r'\1<redacted>', message)
    return message.strip()[:6000]


def transaction(sql, app_role='trading_app', readonly_role='trading_readonly'):
    for role in (app_role, readonly_role):
        if not isinstance(role,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.@-]{0,62}',role):
            raise ValueError('Valid discovered database roles required')
    role_array = "ARRAY["+",".join("'"+r.replace("'","''")+"'" for r in (app_role,readonly_role))+"]"
    readonly_literal = "'"+readonly_role.replace("'","''")+"'"
    if hashlib.sha256(sql.encode()).hexdigest() != CHECKSUM:
        raise ValueError('Migration 065 checksum mismatch; refusing to execute')
    return '''BEGIN;
SET LOCAL statement_timeout = '60s';
SELECT pg_advisory_xact_lock(hashtext('mt5-native-bridge-schema-migrations'));
DO $repair$ BEGIN
IF NOT EXISTS (SELECT 1 FROM platform.schema_migrations WHERE version='064') THEN
 RAISE EXCEPTION 'Prerequisite migration 064 is not applied';
END IF;
IF EXISTS (SELECT 1 FROM platform.schema_migrations
 WHERE version ~ '^[0-9]+$' AND version::numeric=65 AND
 (version <> '065' OR filename <> '065_strategy_library_reader_permissions.sql'
 OR checksum_sha256 <> 'a8a63151ee915be84e1a01b36ae1a852e2a79c3bda0f78068e39cbf1200a9e2e')) THEN
 RAISE EXCEPTION 'Migration 065 ledger conflict';
END IF;
IF NOT EXISTS (SELECT 1 FROM platform.schema_migrations WHERE version='065') THEN
''' + sql + '''
INSERT INTO platform.schema_migrations(version,filename,checksum_sha256)
VALUES ('065','065_strategy_library_reader_permissions.sql',
 'a8a63151ee915be84e1a01b36ae1a852e2a79c3bda0f78068e39cbf1200a9e2e');
END IF;
END $repair$;
DO $readers$ DECLARE r text; BEGIN
FOREACH r IN ARRAY ''' + role_array + ''' LOOP
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r) THEN
  RAISE EXCEPTION 'Discovered application role is missing';
 END IF;
 EXECUTE format('GRANT USAGE ON SCHEMA research TO %I',r);
 EXECUTE format('GRANT SELECT ON research.strategy_library_forward_state, research.strategy_library_forward_event TO %I',r);
END LOOP;
END $readers$;
DO $verify$ DECLARE r text; t text; BEGIN
FOREACH r IN ARRAY ''' + role_array + ''' LOOP
 IF NOT has_schema_privilege(r,'research','USAGE') THEN
  RAISE EXCEPTION 'Reader schema privilege missing';
 END IF;
 FOREACH t IN ARRAY ARRAY['research.strategy_library_forward_state',
                         'research.strategy_library_forward_event'] LOOP
  IF NOT has_table_privilege(r,t,'SELECT') THEN
   RAISE EXCEPTION 'Reader SELECT privilege missing';
  END IF;
  IF r=''' + readonly_literal + ''' AND
    (has_table_privilege(r,t,'INSERT') OR has_table_privilege(r,t,'UPDATE')
      OR has_table_privilege(r,t,'DELETE')) THEN
   RAISE EXCEPTION 'Read-only role unexpectedly has write access';
  END IF;
 END LOOP;
END LOOP;
END $verify$;
COMMIT;
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--sql', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    lines = []

    def run(arguments, payload=None):
        step = ('CONTEXT' if arguments[:1] == ['config'] else
                'ADMIN_MIGRATION_065' if 'trading-postgres-0' in arguments else 'DATABASE_TARGET')
        result = subprocess.run(['kubectl','--kubeconfig',args.kubeconfig,
                                 '--request-timeout=30s',*arguments],
                                input=payload,text=True,capture_output=True,timeout=120)
        if result.returncode:
            detail = safe_error(result.stderr or result.stdout or 'No diagnostic output')
            raise RuntimeError(step+': exit='+str(result.returncode)+'\n'+detail)
        return result.stdout

    try:
        sql = Path(args.sql).read_bytes().decode('utf-8')
        transaction(sql)  # verify pinned migration before any cluster operation
        context = run(['config','current-context']).strip()
        if context != 'local':
            raise RuntimeError('Expected reviewed local context')
        lines.append('Context: local; namespace: trading')
        code = """import json
from postgres.db import connect
with connect(readonly=READ_ONLY) as conn:
 with conn.cursor() as cur:
  cur.execute('SELECT current_database(), current_user')
  database,role=cur.fetchone()
 print(json.dumps({'database':database,'role':role}))
"""
        targets = {}
        for label,deployment,container,readonly in (
            ('resolver','unified-outcome-resolver','resolver',False),
            ('orchestrator','mt5-native-bridge-main-runtime','orchestrator-shadow',False),
            ('api','platform-signals-api','api',True)):
            targets[label]=json.loads(run(['-n','trading','exec','-i','deployment/'+deployment,
                                          '-c',container,'--','python3','-'],
                                         code.replace('READ_ONLY',repr(readonly))))
        target=targets['resolver']['database']
        if not isinstance(target,str) or not target or any(t['database'] != target for t in targets.values()):
            raise RuntimeError('Reader databases do not match verified resolver target')
        body=transaction(sql,targets['orchestrator']['role'],targets['api']['role'])
        lines.append('Database target verified across resolver, orchestrator and API connections')
        lines.append('Discovered orchestrator role: '+targets['orchestrator']['role'])
        lines.append('Discovered API read role: '+targets['api']['role'])
        run(['-n','trading','exec','-i','trading-postgres-0','--','sh','-c',
             'exec psql -X -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1"',
             'sh',target],body)
        lines.extend(['Migration 065 applied/verified through the PostgreSQL admin path',
                      'SHA256: '+CHECKSUM,
                      'API/orchestrator read privileges verified; read-only writes denied',
                      'No workload, ownership, observer checkpoint or execution changes'])
    except Exception as exc:
        lines.append('BLOCKED: '+str(exc))
        Path(args.report).write_text('\n'.join(lines)+'\n')
        print('\n'.join(lines))
        raise SystemExit(1)
    Path(args.report).write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
