"""Fail deployment preflight before traffic reaches incompatible database code."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, DatabaseError
from django.db.migrations.executor import MigrationExecutor


def check_backend_ready():
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if pending:
        names = ', '.join(f'{migration.app_label}.{migration.name}' for migration, _ in pending)
        raise CommandError('Unapplied migrations: ' + names + '. Run manage.py migrate --noinput '
                           'using the web service database configuration before restarting services.')
    # Inspect physical columns too: a faked migration or a different DB must not pass.
    from django_api.models import AgentJob
    try:
        with connection.cursor() as cursor:
            columns = {col.name for col in connection.introspection.get_table_description(
                cursor, AgentJob._meta.db_table)}
    except DatabaseError as exc:
        raise CommandError('Cannot inspect the AgentJob table. Check the web service database configuration.') from exc
    missing = {field.column for field in AgentJob._meta.local_fields} - columns
    if missing:
        raise CommandError('AgentJob database columns missing: ' + ', '.join(sorted(missing))
                           + '. Reconcile database schema and migration history; do not fake migrations.')
    jwt = getattr(settings, 'SIMPLE_JWT', {})
    algorithm = jwt.get('ALGORITHM', 'HS256')
    key = jwt.get('SIGNING_KEY', settings.SECRET_KEY)
    minimum = {'HS256': 32, 'HS384': 48, 'HS512': 64}.get(algorithm)
    if not settings.DEBUG and minimum and len(key.encode('utf-8')) < minimum:
        raise CommandError(f'JWT signing key is too short for {algorithm} (minimum {minimum} bytes). '
                           'Configure a strong random signing key and restart all application processes. '
                           'Rotation invalidates existing tokens. Key value has not been logged.')


class Command(BaseCommand):
    help = 'Check migrations, physical agent-job columns and production JWT key length.'

    def handle(self, *args, **options):
        check_backend_ready()
        self.stdout.write(self.style.SUCCESS('Backend schema and signing-key checks passed.'))
