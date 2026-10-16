import unittest
from scripts.provision_strategy_library_canary import migration_transaction, login_sql, quote


class ProvisionOperatorTests(unittest.TestCase):
    def test_transaction_uses_migration_fence_and_checksum(self):
        sql = migration_transaction('SELECT 1;', 'abc')
        self.assertTrue(sql.startswith('BEGIN;'))
        self.assertTrue(sql.endswith('COMMIT;\n'))
        self.assertIn('mt5-native-bridge-schema-migrations', sql)
        self.assertIn("checksum_sha256 <> 'abc'", sql)
        self.assertIn("VALUES ('063'", sql)

    def test_login_creation_never_rotates_existing_password(self):
        sql = login_sql('test-password')
        self.assertIn('IF NOT EXISTS', sql)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE', sql)
        self.assertNotIn('ALTER ROLE', sql)
        self.assertIn('GRANT strategy_library_observer', sql)
        self.assertIn('shobj_description', sql)

    def test_sql_literal_quoting(self):
        self.assertEqual(quote("a'b"), "'a''b'")

    def test_operator_success_uses_stdin_and_keeps_report_secret_free(self):
        import io,json,subprocess,tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest.mock import patch
        from scripts.provision_strategy_library_canary import main
        calls=[]
        def fake_run(args, **kwargs):
            calls.append((args,kwargs.get('input')))
            tail=args[4:]
            output=''
            if tail==['config','current-context']: output='local\n'
            elif 'deployment/unified-outcome-resolver' in args and "conn.info.host" in (kwargs.get('input') or ''):
                output=json.dumps({'PGHOST':'trading-postgres.trading.svc','PGPORT':'5432',
                                   'PGDATABASE':'trading','canonical_signal_oid':123})
            if 'deployment/unified-outcome-resolver' in args and 'privileges=cur.fetchone()' in (kwargs.get('input') or ''):
                compile(kwargs['input'], '<verification>', 'exec')
            return subprocess.CompletedProcess(args,0,output,'')
        with tempfile.TemporaryDirectory() as tmp:
            sql=Path(tmp)/'063.sql';sql.write_text('SELECT 1;')
            report=Path(tmp)/'report.txt'
            argv=['provision','--kubeconfig','local.yaml','--sql',str(sql),'--report',str(report)]
            with patch('sys.argv',argv),patch('subprocess.run',side_effect=fake_run),patch('secrets.token_urlsafe',return_value='fake-secret-for-test'),redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped: main()
            self.assertEqual(stopped.exception.code,0)
            self.assertIn('PROVISIONED',report.read_text())
            self.assertNotIn('fake-secret-for-test',report.read_text())
            self.assertTrue(any('PASSWORD' in (stdin or '') for _,stdin in calls))
            self.assertTrue(all('fake-secret-for-test' not in ' '.join(args) for args,_ in calls))
