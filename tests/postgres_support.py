"""PostgreSQL test helpers shared by the database tests (fixtures live in conftest.py)."""

from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo


def restricted_url(database):
    """A non-superuser login, since superusers bypass row-level security."""
    role = f"carenote_app_{uuid4().hex[:8]}"
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'test-only'")
        connection.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}")
    return make_conninfo(database, user=role, password="test-only")
