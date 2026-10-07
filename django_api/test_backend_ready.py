from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django_api.models import AgentJob
from django_api.management.commands.check_backend_ready import check_backend_ready


@override_settings(DEBUG=False, SECRET_KEY='a' * 64, SIMPLE_JWT={})
class BackendReadyTests(SimpleTestCase):
    def probe(self, pending=False, missing=False):
        module = 'django_api.management.commands.check_backend_ready'
        with patch(module + '.MigrationExecutor') as executor, patch(module + '.connection') as connection:
            executor.return_value.migration_plan.return_value = [
                (SimpleNamespace(app_label='django_api', name='0015_data_retention_hold'), False)
            ] if pending else []
            connection.introspection.get_table_description.return_value = [
                SimpleNamespace(name=field.column) for field in AgentJob._meta.local_fields
                if not (missing and field.column == 'retention_checked_at')]
            check_backend_ready()

    def test_pending_migration_fails_with_actionable_message(self):
        with self.assertRaisesMessage(CommandError, 'manage.py migrate --noinput'):
            self.probe(pending=True)

    def test_fake_migration_does_not_hide_missing_physical_column(self):
        with self.assertRaisesMessage(CommandError, 'retention_checked_at'):
            self.probe(missing=True)

    @override_settings(SECRET_KEY='secret7')
    def test_short_production_key_fails_without_logging_it(self):
        with self.assertRaises(CommandError) as error:
            self.probe()
        self.assertIn('minimum 32 bytes', str(error.exception))
        self.assertNotIn('secret7', str(error.exception))

    def test_ready_schema_and_key_pass(self):
        self.probe()


@override_settings(DEBUG=False, SECRET_KEY='a' * 64, SIMPLE_JWT={})
class MigratedBackendReadyTests(TestCase):
    def test_real_migrated_schema_passes_and_retention_column_is_queryable(self):
        check_backend_ready()
        self.assertEqual(list(AgentJob.objects.values_list('retention_checked_at', flat=True)), [])
