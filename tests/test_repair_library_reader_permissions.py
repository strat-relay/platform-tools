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
                '{"database":"trading_test"}' if 'python3' in args else 'COMMIT\n')
            return subprocess.CompletedProcess(args,0,output,'')
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'report.txt'
            with patch.object(sys,'argv',['repair','--kubeconfig','config.yaml','--sql',str(SQL),
                                          '--report',str(report)]), patch.object(subprocess,'run',side_effect=kubectl):
                repair.main()
            self.assertIn('read privileges verified',report.read_text())
        self.assertEqual(len(calls),3)
        admin,options=calls[-1]
        self.assertIn('trading-postgres-0',admin)
        self.assertEqual(admin[-1],'trading_test')
        self.assertIn('BEGIN;',options['input'])
        self.assertFalse(any(action in args for args,_ in calls
                             for action in ('apply','patch','delete','rollout','scale')))
