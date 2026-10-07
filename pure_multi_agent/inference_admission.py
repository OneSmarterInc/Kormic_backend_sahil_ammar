"""Cross-process FIFO admission with bounded priority advantage and tenant limits."""
import logging
import time
from contextvars import ContextVar
from contextlib import contextmanager, ExitStack
from datetime import timedelta
from django.utils import timezone
from django_api.models import InferenceWaiter, InferenceTenant
from pure_multi_agent.database_capacity import lease
from pure_multi_agent.capacity import AgentBusy
from kormic_backend.ollama_config import QWEN_SLOT_TTL_SECONDS

workload = ContextVar('inference_workload', default=None)
logger = logging.getLogger(__name__)


def admission_poll_delay(elapsed):
    """Bound DB polling while preserving quick admission for short waits."""
    if elapsed < 0.6:
        return 0.15
    if elapsed < 3:
        return 0.35
    if elapsed < 8:
        return 0.65
    return 1.0


def tenant_key(owner):
    # All university presenter conversations share a fair-service budget.
    return ':'.join(owner.split(':')[:2])


def choose_waiter(provider, now):
    from agent_queries.models import AgentCapacitySlot
    rows = list(InferenceWaiter.objects.filter(provider=provider).order_by('rank_at', 'id')[:1000])
    active = set(AgentCapacitySlot.objects.filter(key__startswith='inference-tenant:' + provider + ':',
        token__isnull=False, expires_at__gt=now).values_list('key', flat=True))
    rows = [row for row in rows if 'inference-tenant:' + provider + ':' + row.tenant not in active]
    if AgentCapacitySlot.objects.filter(key='inference-background:' + provider, token__isnull=False, expires_at__gt=now).exists():
        rows = [row for row in rows if row.priority < 20]
    if not rows:
        return None
    # Background rank includes a 20s offset. Promote it after 25s, before
    # the 30s admission deadline, rather than expiring before aging can apply.
    aged = [row for row in rows if row.rank_at <= now-timedelta(seconds=5)]
    if aged:
        return aged[0]
    served = dict(InferenceTenant.objects.filter(provider=provider, tenant__in={row.tenant for row in rows}).values_list('tenant', 'last_served_at'))
    epoch = now-timedelta(days=36500)
    return min(rows, key=lambda row: (row.priority, served.get(row.tenant) or epoch, row.rank_at, str(row.pk)))


@contextmanager
def chat_workload(owner):
    with workload_scope(owner, 0):
        yield


@contextmanager
def workload_scope(owner, priority=10):
    token = workload.set((owner, priority))
    try:
        yield
    finally:
        workload.reset(token)


@contextmanager
def admission(provider, run, estimate, acquire):
    from github_profiles.scheduling import CapacityBusy
    import uuid
    owner, priority = workload.get() or (('github:' + str(run.profile_id), 20) if run else ('interactive:' + str(uuid.uuid4()), 10))
    tenant = tenant_key(owner)
    now = timezone.now()
    ticket = InferenceWaiter.objects.create(provider=provider, owner=owner,
        tenant=tenant, priority=priority, rank_at=now + timedelta(seconds=priority), expires_at=now + timedelta(seconds=40))
    started = time.monotonic()
    stack = ExitStack()
    try:
        while True:
            try:
                # Only short admission decisions are serialized. Network I/O is outside this lease.
                with lease('inference-admit:' + provider, ttl=5):
                    InferenceWaiter.objects.filter(expires_at__lte=timezone.now()).delete()
                    first = choose_waiter(provider, timezone.now())
                    if first and first.pk == ticket.pk:
                        attempt = ExitStack()
                        try:
                            ttl = QWEN_SLOT_TTL_SECONDS if provider == 'qwen' else 600
                            attempt.enter_context(lease('inference-tenant:' + provider + ':' + tenant, ttl=ttl))
                            if priority >= 20:
                                attempt.enter_context(lease('inference-background:' + provider, ttl=ttl))
                            attempt.enter_context(acquire(provider, run, estimate))
                        except BaseException:
                            attempt.close()
                            raise
                        stack.enter_context(attempt.pop_all())
                        InferenceTenant.objects.update_or_create(provider=provider, tenant=tenant,
                            defaults={'last_served_at': timezone.now()})
                        ticket.delete()
                        break
            except (AgentBusy, CapacityBusy):
                pass
            elapsed = time.monotonic() - started
            if elapsed >= 30:
                raise CapacityBusy('Waiting for shared ' + provider + ' capacity')
            time.sleep(min(admission_poll_delay(elapsed), 30 - elapsed))
        admitted = time.monotonic()
        try:
            yield
        finally:
            logger.info('inference provider=%s workload=%s wait_seconds=%.3f execution_seconds=%.3f',
                provider, priority, admitted-started, time.monotonic()-admitted)
    finally:
        stack.close()
        InferenceWaiter.objects.filter(pk=ticket.pk).delete()
