from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from kormic_backend.celery import _close_old_db_connections


class CeleryConnectionCleanupTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    @mock.patch("django.db.close_old_connections")
    def test_eager_mode_does_not_close_callers_connection(self, close_old):
        task = SimpleNamespace(request=SimpleNamespace(is_eager=True))
        _close_old_db_connections(task=task)
        close_old.assert_not_called()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @mock.patch("django.db.close_old_connections")
    def test_worker_task_still_closes_old_connections(self, close_old):
        task = SimpleNamespace(request=SimpleNamespace(is_eager=False))
        _close_old_db_connections(task=task)
        close_old.assert_called_once_with()
