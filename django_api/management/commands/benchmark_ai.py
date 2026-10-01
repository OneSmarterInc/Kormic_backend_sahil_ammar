"""Synthetic concurrency probe: no real profiles, secrets or external fallback."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
import httpx
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.conf import settings
from github_profiles.scheduling import model_slot
from pure_multi_agent.inference_admission import chat_workload


def probe(index):
    close_old_connections()
    started = time.monotonic()
    try:
        with chat_workload('benchmark:' + str(index)), model_slot('qwen', None, 500):
            admitted = time.monotonic()
            with httpx.Client(trust_env=False, timeout=120) as client:
                response = client.post(settings.GITHUB_OLLAMA_BASE_URL.rstrip('/') + '/api/chat', json={
                    'model': settings.GITHUB_OLLAMA_MODEL, 'stream': False, 'think': False,
                    'messages': [{'role':'user', 'content':f'Synthetic request {index}. In one sentence explain why universities have admission requirements.'}],
                    'options': {'num_ctx': 16384, 'num_predict': 60, 'temperature': 0}, 'keep_alive': -1})
                response.raise_for_status()
                data = response.json()
            return {'request':index, 'success':bool(data.get('message', {}).get('content')),
                'wait_seconds':round(admitted-started, 3), 'total_seconds':round(time.monotonic()-started, 3),
                'tokens':data.get('eval_count')}
    except Exception as exc:
        return {'request':index, 'success':False, 'error_type':type(exc).__name__}
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = 'Run ten synthetic Qwen-only requests through shared admission.'
    def handle(self, *args, **options):
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(probe, range(10)))
        self.stdout.write(json.dumps({'seconds':round(time.monotonic()-started,3),
            'slots':settings.GITHUB_QWEN_CONCURRENCY, 'results':results}, indent=2))
