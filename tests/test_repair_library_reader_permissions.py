import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import repair_library_reader_permissions as repair

SQL = Path(__file__).resolve().parents[1]/'postgres/migrations'/repair.FILENAME


class RepairTests(unittest.TestCase):
    def test_custom_production_roles_are_used_without_creating_assumed_roles(self):
        body=repair.transaction(SQL.read_text(),'actual_app','actual_api_reader')
        self.assertIn("FOREACH r IN ARRAY ARRAY['actual_app','actual_api_reader']",body)
        self.assertIn("IF r='actual_api_reader' AND",body)
        self.assertIn("EXECUTE format('GRANT USAGE ON SCHEMA research TO %I',r)",body)
        self.assertNotIn('CREATE ROLE',body)
        with self.assertRaises(ValueError):
            repair.transaction(SQL.read_text(),'app','$readers$')

    def test_failure_diagnostics_preserve_sql_error_and_redact_credentials(self):
        detail=repair.safe_error('ERROR: permission denied for table x password="secret value" '
                                 'token=abc Bearer xyz postgresql://user:pwd@host/db')
        self.assertIn('permission denied for table x', detail)
        for secret in ('secret value','abc','xyz','user:pwd'):
            self.assertNotIn(secret, detail)

    def test_admin_failure_records_step_and_psql_error(self):
        def kubectl(args,**kwargs):
            if 'trading-postgres-0' in args:
                return subprocess.CompletedProcess(args,3,'','ERROR: repair verification failed')
            output='local\n' if args[-2:]==['config','current-context'] else '{"database":"trading_test","role":"custom_reader"}'
            return subprocess.CompletedProcess(args,0,output,'')
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.txt'
            with patch.object(sys,'argv',['repair','--kubeconfig','config.yaml','--sql',str(SQL),
                                          '--report',str(report)]), patch.object(subprocess,'run',side_effect=kubectl):
                with self.assertRaises(SystemExit): repair.main()
            self.assertIn('ADMIN_MIGRATION_065: exit=3',report.read_text())
            self.assertIn('ERROR: repair verification failed',report.read_text())

    def test_only_the_exact_merged_migration_is_accepted(self):
        sql=SQL.read_bytes().decode('utf-8')
        self.assertEqual(hashlib.sha256(sql.encode()).hexdigest(),repair.CHECKSUM)
        body=repair.transaction(sql)
        self.assertIn("hashtext('mt5-native-bridge-schema-migrations')",body)
        self.assertIn('INSERT INTO platform.schema_migrations',body)
        self.assertIn('Migration 065 ledger conflict',body)
        self.assertIn('END $verify$;\nCOMMIT;',body)
        with self.assertRaises(ValueError): repair.transaction(sql+'\n')

    def test_operator_flow_uses_admin_stdin_and_changes_no_workloads(self):
        calls=[]
        def kubectl(args,**kwargs):
            calls.append((args,kwargs))
            output='local\n' if args[-2:]==['config','current-context'] else (
                '{"database":"trading_test","role":"custom_reader"}' if 'python3' in args else 'COMMIT\n')
            return subprocess.CompletedProcess(args,0,output,'')
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.txt'
            with patch.object(sys,'argv',['repair','--kubeconfig','config.yaml','--sql',str(SQL),
                                          '--report',str(report)]), patch.object(subprocess,'run',side_effect=kubectl):
                repair.main()
            self.assertIn('read privileges verified',report.read_text())
        self.assertEqual(len(calls),5)
        admin,options=calls[-1]
        self.assertIn('trading-postgres-0',admin)
        self.assertEqual(admin[-1],'trading_test')
        self.assertIn('BEGIN;',options['input'])
        self.assertIn("ARRAY['custom_reader','custom_reader']",options['input'])
        self.assertFalse(any(action in args for args,_ in calls
                             for action in ('apply','patch','delete','rollout','scale')))
