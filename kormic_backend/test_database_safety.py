from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from django.test import SimpleTestCase


class ProductionDatabaseGuardTests(SimpleTestCase):
    def _settings_import(self, extra_env=None, remove=()):
        env = os.environ.copy()
        for key in remove:
            env.pop(key, None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-c", "import kormic_backend.settings"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_production_without_db_engine_fails_closed(self):
        result = self._settings_import(
            {"DJANGO_DEBUG": "false"},
            remove=("DB_ENGINE", "POSTGRES_PASSWORD"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_ENGINE=postgresql", result.stderr + result.stdout)

    def test_production_postgres_without_password_fails_closed(self):
        result = self._settings_import(
            {"DJANGO_DEBUG": "false", "DB_ENGINE": "postgresql"},
            remove=("POSTGRES_PASSWORD",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD", result.stderr + result.stdout)


class SQLiteCheckpointerPersistenceTests(SimpleTestCase):
    def test_separate_saver_instances_share_persisted_state(self):
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import StateGraph

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent-checkpoints.sqlite3")

            builder = StateGraph(dict)
            builder.add_node("remember", lambda state: {"message": state["message"]})
            builder.set_entry_point("remember")
            builder.set_finish_point("remember")
            config = {"configurable": {"thread_id": "student-1"}}

            conn1 = sqlite3.connect(path, check_same_thread=False)
            saver1 = SqliteSaver(conn1)
            saver1.setup()
            graph1 = builder.compile(checkpointer=saver1)
            graph1.invoke({"message": "first turn"}, config)
            conn1.close()

            conn2 = sqlite3.connect(path, check_same_thread=False)
            saver2 = SqliteSaver(conn2)
            saver2.setup()
            graph2 = builder.compile(checkpointer=saver2)
            snapshot = graph2.get_state(config)
            conn2.close()

            self.assertEqual(snapshot.values.get("message"), "first turn")
