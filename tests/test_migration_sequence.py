import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from postgres.db import apply_migrations, MIGRATIONS


class MigrationSequenceTests(unittest.TestCase):
    def test_repository_migration_versions_are_unique(self):
        paths = sorted(MIGRATIONS.glob('*.sql'))
        versions = [p.name.split('_', 1)[0] for p in paths]
        self.assertEqual(len(versions), len(set(versions)), 'Concurrent migrations must use distinct versions')

    def test_duplicate_versions_fail_before_any_database_statement(self):
        conn = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'072_first.sql').write_text('CREATE TABLE forbidden_first(id int);')
            (root/'072_second.sql').write_text('CREATE TABLE forbidden_second(id int);')
            with self.assertRaisesRegex(RuntimeError, 'Duplicate migration version 072'):
                apply_migrations(conn, root)
        conn.cursor.assert_not_called()
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()
