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
