"""Durable Windows pilot worker. Production can use the same task via Celery."""
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from django_api.models import AgentJob
from pure_multi_agent.tasks import execute_agent_job

logger = logging.getLogger(__name__)


def execute(pk):
    close_old_connections()
    try:
        execute_agent_job.run(str(pk))
    finally:
        close_old_connections()


class Command(BaseCommand):
    help = 'Process persisted chat jobs without a Redis broker (local pilot).'

    def add_arguments(self, parser):
        parser.add_argument('--concurrency', type=int, default=10)
        parser.add_argument('--queue', choices=['chat','documents'], default='chat')

    def handle(self, *args, **options):
        if settings.AGENT_QUEUE_BACKEND != 'database' or not settings.AGENT_DISTRIBUTED_LIMITS:
            raise CommandError('Set AGENT_QUEUE_BACKEND=database and AGENT_DISTRIBUTED_LIMITS=true.')
        size = max(1, min(32, options['concurrency']))
        active = {}
        with ThreadPoolExecutor(max_workers=size) as pool:
            while True:
                for pk, future in list(active.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception:
                            logger.exception('Chat worker failed job=%s', pk)
                        del active[pk]
                close_old_connections()
                now = timezone.now()
                from pure_multi_agent.job_recovery import recover
                recover()
                AgentJob.objects.filter(status='queued', created_at__lt=now-timedelta(seconds=settings.AGENT_QUEUE_TIMEOUT)).update(
                    status='failed', completed_at=now, error='Queue wait expired. Please retry.')
                from pure_multi_agent.jobs import ready_jobs
                for pk in ready_jobs(size-len(active), active, options['queue']):
                    active[pk] = pool.submit(execute, pk)
                time.sleep(.5)
