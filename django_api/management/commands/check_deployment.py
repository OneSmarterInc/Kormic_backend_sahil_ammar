"""Exercise infrastructure from the same network/environment as the web API."""
import json
import uuid
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import caches
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from kormic_backend.celery import app


class Command(BaseCommand):
    help = 'Verify PostgreSQL, Redis, all Celery queues, and Claude configuration; optionally generate text.'

    def add_arguments(self, parser):
        parser.add_argument('--model', action='store_true', help='Also run one small paid Claude inference request.')
        parser.add_argument('--timeout', type=int, default=120)

    def handle(self, *args, **options):
        from redis import Redis

        timeout = options['timeout']
        if timeout < 1:
            raise CommandError('timeout must be positive')
        from .check_backend_ready import check_backend_ready
        check_backend_ready()
        self.stdout.write('PASS database schema and signing-key checks')
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            assert cursor.fetchone()[0] == 1
        self.stdout.write('PASS database connection')
        for name, url in (
            ('broker', settings.CELERY_BROKER_URL),
            ('results', settings.CELERY_RESULT_BACKEND),
            ('capacity', settings.AGENT_REDIS_URL),
        ):
            client = Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
            try:
                if not client.ping():
                    raise CommandError(f'{name} Redis did not respond')
            finally:
                client.close()
            self.stdout.write(f'PASS Redis {name}')
        for alias in ('default', 'agent_config'):
            key = 'deployment-probe:' + uuid.uuid4().hex
            cache = caches[alias]
            try:
                cache.set(key, key, timeout=30)
                if cache.get(key) != key:
                    raise CommandError(f'{alias} cache round trip failed')
            finally:
                cache.delete(key)
            self.stdout.write(f'PASS cache {alias}')
        for queue in ('celery', 'agent_chat', 'agent_documents', 'knowledge_index'):
            nonce = uuid.uuid4().hex
            result = app.send_task('kormic.deployment_probe', args=[nonce], queue=queue, expires=timeout)
            try:
                if result.get(timeout=timeout) != nonce:
                    raise CommandError(f'{queue} returned an unexpected result')
            except Exception as exc:
                raise CommandError(f'{queue} did not complete its worker/result round trip ({type(exc).__name__})') from exc
            finally:
                result.forget()
            self.stdout.write(f'PASS Celery queue {queue}')

        import os
        from pure_multi_agent.claude_policy import model_name
        if not os.getenv('ANTHROPIC_API_KEY'):
            raise CommandError('ANTHROPIC_API_KEY is missing')
        self.stdout.write(f'PASS Claude configured: {model_name()} (credentials not yet verified)')
        if options['model']:
            from pure_multi_agent.model_router import invoke
            from langchain_core.messages import HumanMessage
            from pure_multi_agent.telemetry import diagnostic_scope
            with diagnostic_scope():
                reply = invoke([HumanMessage(content='Reply ready only.')], profile='routing', single_attempt=True)
            if not reply.content:
                raise CommandError('Claude did not complete text generation')
            self.stdout.write('PASS real Claude inference')
        self.stdout.write(self.style.SUCCESS('Deployment checks passed. Test login, uploads and agent workflows separately.'))
