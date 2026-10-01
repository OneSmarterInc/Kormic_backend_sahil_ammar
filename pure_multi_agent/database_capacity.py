"""Shared database limits for single-machine installations without Redis."""
from contextlib import contextmanager
from datetime import timedelta
import time
import uuid
from django.db.models import F, Q
from django.utils import timezone
from agent_queries.models import AgentCapacitySlot, AgentRateWindow


@contextmanager
def lease(key, limit=1, ttl=900, wait=0):
    from pure_multi_agent.capacity import AgentBusy
    token, deadline, acquired = uuid.uuid4(), time.monotonic() + wait, None
    for number in range(limit):
        AgentCapacitySlot.objects.get_or_create(key=key, number=number)
    while acquired is None:
        now = timezone.now()
        for number in range(limit):
            changed = AgentCapacitySlot.objects.filter(key=key, number=number).filter(
                Q(token__isnull=True) | Q(expires_at__lte=now)).update(token=token, expires_at=now+timedelta(seconds=ttl))
            if changed:
                acquired = number
                break
        if acquired is not None:
            break
        if time.monotonic() >= deadline:
            raise AgentBusy('Agent capacity is busy; please retry shortly.')
        time.sleep(0.1)
    try:
        yield
    finally:
        AgentCapacitySlot.objects.filter(key=key, number=acquired, token=token).update(token=None, expires_at=None)


def check_rate(key, limit, seconds=60):
    from pure_multi_agent.capacity import AgentBusy
    now = timezone.now()
    AgentRateWindow.objects.get_or_create(key=key, defaults={'started_at': now})
    AgentRateWindow.objects.filter(key=key, started_at__lte=now-timedelta(seconds=seconds)).update(started_at=now, count=0)
    if not AgentRateWindow.objects.filter(key=key, count__lt=limit).update(count=F('count')+1):
        raise AgentBusy('Request rate exceeded. Please retry shortly.')
