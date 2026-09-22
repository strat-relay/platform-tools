from __future__ import annotations

import os
import psycopg
from psycopg import sql

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect


def main() -> None:
    app_role = os.environ["P2_APP_USER"]
    app_password = os.environ["P2_APP_PASSWORD"]
    with connect(PostgresConfig.from_env()) as conn:
        applied = apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app_role,))
            exists = cur.fetchone() is not None
            statement = (sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}") if exists else sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}"))
            cur.execute(statement.format(sql.Identifier(app_role), sql.Literal(app_password)))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}" ).format(sql.Identifier(os.environ["PGDATABASE"]), sql.Identifier(app_role)))
            for schema in ("platform", "strategy"):
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}" ).format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {} TO {}" ).format(sql.Identifier(schema), sql.Identifier(app_role)))
                cur.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}" ).format(sql.Identifier(schema), sql.Identifier(app_role)))
            cur.execute("SELECT version FROM platform.schema_migrations ORDER BY version")
            versions = [row[0] for row in cur.fetchall()]
        print({"applied": applied, "schema_versions": versions, "app_role_created_or_updated": True})


if __name__ == "__main__":
    main()
