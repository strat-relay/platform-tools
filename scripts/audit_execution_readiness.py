"""Print redacted read-only diagnostics using the existing configured DB connection."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    try:
        from postgres.db import connect
        from orchestration.config import load_config_from_database
        from orchestration.execution_readiness import execution_readiness
        with connect(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SELECT current_database(), NOT pg_is_in_recovery()')
                database, primary = cur.fetchone()
            report = execution_readiness(load_config_from_database(conn))
            report['database'] = database
            report['primary'] = primary
            conn.rollback()
        print(json.dumps(report, indent=2))
    except Exception as exc:
        # Connection errors can include credentials. Export only the exception category.
        print(json.dumps({'read_only': True, 'broker_writes': 0,
                          'error_category': type(exc).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
