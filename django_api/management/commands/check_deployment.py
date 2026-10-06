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
    help = 'Verify PostgreSQL, Redis, all Celery queues, and Ollama; optionally generate text.'

    def add_arguments(self, parser):
        parser.add_argument('--model', action='store_true', help='Also run real Qwen inference (may be slow).')
        parser.add_argument('--timeout', type=int, default=120)

    def handle(self, *args, **options):
        from redis import Redis

        timeout = options['timeout']
        if timeout < 1:
            raise CommandError('timeout must be positive')
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

        base = settings.GITHUB_OLLAMA_BASE_URL.rstrip('/')
        model = settings.GITHUB_OLLAMA_MODEL
        with urlopen(base + '/api/tags', timeout=10) as response:
            models = json.load(response)['models']
        if model not in {row['name'] for row in models}:
            raise CommandError(f'Ollama model {model} has not been pulled')
        self.stdout.write(f'PASS Ollama model present: {model}')
        if options['model']:
            # Match the actual agent context allocation; a tiny probe must not
            # falsely certify memory capacity for a 16K-context agent request.
            body = {'model': model, 'prompt': 'Reply with the word ready.',
                    'stream': False, 'think': False, 'keep_alive': '2m',
                    'options': {'num_ctx': 16384, 'num_predict': 16}}
            request = Request(base + '/api/generate', data=json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            if not result.get('response', '').strip() or not result.get('done'):
                raise CommandError('Ollama did not complete text generation')
            self.stdout.write('PASS real Qwen inference at agent context size')
        self.stdout.write(self.style.SUCCESS('Deployment checks passed. Test login, uploads and agent workflows separately.'))
