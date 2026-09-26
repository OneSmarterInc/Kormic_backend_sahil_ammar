"""Shared DB scheduling: short atomic claims, renewable leases and fair slices."""
import uuid
import time
import threading
from functools import wraps
from contextlib import contextmanager, nullcontext
from datetime import timedelta

from django.conf import settings
from django.db import transaction, OperationalError, connection
from django.db.models import Q, F
from django.utils import timezone
from django_api.models import GitHubSyncRun, GitHubModelPool, GitHubModelSlot
from .errors import ServiceError

_sqlite_writes = threading.RLock()


def database_write_lock():
    # SQLite has one writer. Serialize only short local write transactions,
    # never model/network work. PostgreSQL uses its own row locks. Database
    # compare-and-swap/leases remain authoritative across separate processes.
    return _sqlite_writes if connection.vendor == 'sqlite' else nullcontext()


class LeaseLost(ServiceError):
    pass


class CapacityBusy(Exception):
    """Reschedule without spending retries or falling through to a paid provider."""
    def __init__(self, message='Waiting for model capacity', delay=5):
        super().__init__(message)
        self.delay = delay


def retry_database(method):
    """Retry only short, atomic DB operations; never replay network calls here."""
    @wraps(method)
    def call(*args, **kwargs):
        for attempt in range(7):
            try:
                with database_write_lock():
                    return method(*args, **kwargs)
            except OperationalError:
                if attempt == 6:
                    raise
                time.sleep(0.02 * 2**attempt)
    return call


def owned(run):
    return GitHubSyncRun.objects.filter(pk=run.pk, lease_token=run.lease_token,
        status='running', lease_expires_at__gt=timezone.now(),
        profile__connection__github_user_id=run.profile.github_user_id)


@contextmanager
def fenced(run):
    # Updating before reading also obtains a write lock on SQLite.
    with database_write_lock(), transaction.atomic():
        if not run.lease_token or not owned(run).update(updated_at=timezone.now()):
            raise LeaseLost('The extraction lease ended. Another worker can resume saved progress.')
        yield


@retry_database
def claim(run_id=None):
    now = timezone.now()
    available = GitHubSyncRun.objects.filter(status__in=['queued', 'running'], available_at__lte=now).filter(
        Q(lease_token__isnull=True) | Q(lease_expires_at__lte=now))
    if run_id:
        available = available.filter(pk=run_id)
    # Compare-and-swap is portable; each successful claim excludes all contenders.
    for pk in available.order_by('available_at', 'created_at').values_list('pk', flat=True)[:20]:
        token = uuid.uuid4()
        with transaction.atomic():
            if available.filter(pk=pk).update(status='running', lease_token=token,
                    lease_expires_at=now+timedelta(seconds=settings.GITHUB_JOB_LEASE_SECONDS), updated_at=now):
                # Loading the claimed job is in the same transaction. A read
                # failure cannot leave an orphaned lease no worker received.
                return GitHubSyncRun.objects.select_related('profile__connection').get(pk=pk, lease_token=token)
    return None


@retry_database
def heartbeat(run):
    now = timezone.now()
    if not owned(run).update(lease_expires_at=now+timedelta(seconds=settings.GITHUB_JOB_LEASE_SECONDS), updated_at=now):
        raise LeaseLost('GitHub extraction ownership changed.')


@retry_database
def release(run, delay=0, **fields):
    with fenced(run):
        owned(run).update(lease_token=None, lease_expires_at=None,
            available_at=timezone.now()+timedelta(seconds=delay), **fields)


def recover_legacy_runs():
    # Old pre-lease workers cannot safely be resumed: they have no checkpoint.
    GitHubSyncRun.objects.filter(status='running', lease_token__isnull=True,
        updated_at__lt=timezone.now()-timedelta(minutes=15), stage='collect', work={}).update(
            status='queued', progress='Resuming interrupted collection')


def provider_blocked(provider):
    return GitHubModelPool.objects.filter(provider=provider, blocked_until__gt=timezone.now()).exists()


def block_provider(provider, seconds=60):
    GitHubModelPool.objects.get_or_create(provider=provider)
    GitHubModelPool.objects.filter(provider=provider).update(blocked_until=timezone.now()+timedelta(seconds=seconds))


@contextmanager
def model_slot(provider, run, estimated_tokens):
    limit = getattr(settings, 'GITHUB_' + provider.upper() + '_CONCURRENCY')
    if limit < 1:
        raise CapacityBusy(provider + ' inference is paused', 30)
    now, token, selected = timezone.now(), uuid.uuid4(), None
    for number in range(limit):
        GitHubModelSlot.objects.get_or_create(provider=provider, number=number)
        slot = GitHubModelSlot.objects.filter(provider=provider, number=number).filter(Q(token__isnull=True) | Q(expires_at__lte=now))
        if slot.update(token=token, expires_at=now+timedelta(seconds=600)):
            selected = number
            break
    if selected is None:
        raise CapacityBusy('Waiting for ' + provider + ' capacity')
    try:
        GitHubModelPool.objects.get_or_create(provider=provider)
        with (fenced(run) if run is not None else transaction.atomic()):
            GitHubModelPool.objects.filter(provider=provider).update(requests=F('requests')+0)
            pool = GitHubModelPool.objects.get(provider=provider)
            if pool.window_started_at <= now-timedelta(minutes=1):
                pool.window_started_at, pool.requests, pool.reserved_tokens = now, 0, 0
            rpm = getattr(settings, 'GITHUB_' + provider.upper() + '_RPM')
            tpm = getattr(settings, 'GITHUB_' + provider.upper() + '_TPM')
            if pool.requests >= rpm or pool.reserved_tokens+estimated_tokens > tpm:
                raise CapacityBusy('Waiting for ' + provider + ' rate budget', 15)
            if run is not None:
                current = GitHubSyncRun.objects.get(pk=run.pk)
                if current.model_calls >= settings.GITHUB_RUN_MAX_MODEL_CALLS or current.reserved_tokens+estimated_tokens > settings.GITHUB_RUN_MAX_TOKENS:
                    raise ServiceError('This extraction reached its AI budget. Collected facts and completed reports are retained.')
            pool.requests += 1
            pool.reserved_tokens += estimated_tokens
            pool.save()
            if run is not None:
                owned(run).update(model_calls=F('model_calls')+1, reserved_tokens=F('reserved_tokens')+estimated_tokens)
        yield
    finally:
        GitHubModelSlot.objects.filter(provider=provider, number=selected, token=token).update(token=None, expires_at=None)
