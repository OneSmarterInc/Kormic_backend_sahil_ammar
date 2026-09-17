# kormic_backend/test_runner.py
# pure_multi_agent.runtime opens its own psycopg connection pool to the
# same database Django's ORM uses (see runtime.py's _build_checkpointer),
# separate from django.db.connections because it's a different driver
# (psycopg v3, required by langgraph-checkpoint-postgres) with its own
# pooling. Django's test runner doesn't know about that pool, so its
# min_size=1 warm connection is still open when teardown_databases() tries
# to DROP the test database -- Postgres refuses to drop a database with
# other sessions attached, and every test run after the first would fail
# with "database is being accessed by other users". Closing the pool here,
# right before Django asks Postgres to drop the database, is the fix.
from __future__ import annotations

from django.test.runner import DiscoverRunner


class KormicTestRunner(DiscoverRunner):
    def teardown_databases(self, old_config, **kwargs):
        import sys

        runtime_module = sys.modules.get("pure_multi_agent.runtime")
        if runtime_module is not None:
            runtime_module._checkpointer.conn.close()

        super().teardown_databases(old_config, **kwargs)
