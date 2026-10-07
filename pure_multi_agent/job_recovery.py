"""Recover read/model checkpoints; fence late workers and never replay uncertain writes."""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import timedelta
import threading
import json
from django.core.serializers.json import DjangoJSONEncoder
from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q, F
from django.utils import timezone
from django_api.models import AgentJob

execution = ContextVar('agent_job_execution', default=None)


class ExecutionLost(RuntimeError):
    pass


def boundary(ctx=None, keys=(), phase='unsafe'):
    current = execution.get()
    if current is None:
        return
    job_id, token = current
    rows = AgentJob.objects.filter(pk=job_id, execution_token=token, status='processing')
    fields = {'recovery_phase': phase, 'heartbeat_at': timezone.now()}
    if ctx is not None:
        job = rows.first()
        if job is None:
            raise ExecutionLost('Worker ownership changed')
        state = {k: list(ctx[k]) if isinstance(ctx[k], set) else ctx[k] for k in keys if k in ctx}
        # ORM evidence includes dates, decimals and UUIDs. Persist their standard
        # JSON forms without mutating the live context or discarding evidence.
        fields['payload'] = json.loads(json.dumps({**job.payload, 'resume_state': state}, cls=DjangoJSONEncoder))
    if not rows.update(**fields):
        raise ExecutionLost('Worker ownership changed')


@contextmanager
def track(job_id, token):
    stopped = threading.Event()
    def heartbeat():
        try:
            while not stopped.wait(15):
                close_old_connections()
                if not AgentJob.objects.filter(pk=job_id, execution_token=token, status='processing').update(heartbeat_at=timezone.now()):
                    return
        finally:
            close_old_connections()
    marker = execution.set((job_id, token))
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)
        execution.reset(marker)


def recover():
    now = timezone.now()
    # Wait beyond the conversation lease: a replacement cannot overlap its predecessor.
    stale = AgentJob.objects.filter(status='processing', started_at__lt=now-timedelta(seconds=settings.AGENT_JOB_TIMEOUT + 120)).filter(
        Q(heartbeat_at__isnull=True) | Q(heartbeat_at__lt=now-timedelta(seconds=90)))
    safe = stale.filter(recovery_phase='model', recovery_attempts__lt=3)
    safe.update(status='queued', execution_token=None, dispatched_at=None,
        recovery_attempts=F('recovery_attempts')+1)
    stale.update(status='failed', execution_token=None, completed_at=now,
        error='Processing was interrupted during an action. Check saved changes before retrying.')
