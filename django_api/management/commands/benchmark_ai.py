"""Small paid Claude concurrency probe with synthetic input only."""
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
        from pure_multi_agent.model_router import invoke
        from pure_multi_agent.telemetry import diagnostic_scope
        from langchain_core.messages import HumanMessage
        with diagnostic_scope(), chat_workload('benchmark:' + str(index)):
            reply = invoke([HumanMessage(content='Reply ready only.')], profile='routing', single_attempt=True)
            return {'request': index, 'success': bool(reply.content),
                'total_seconds': round(time.monotonic()-started, 3), 'usage': reply.usage_metadata}
    except Exception as exc:
        return {'request':index, 'success':False, 'error_type':type(exc).__name__}
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = 'Run three small paid Claude requests through shared admission.'
    def handle(self, *args, **options):
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(probe, range(3)))
        self.stdout.write(json.dumps({'seconds':round(time.monotonic()-started,3),
            'slots':settings.GITHUB_CLAUDE_CONCURRENCY, 'results':results}, indent=2))
