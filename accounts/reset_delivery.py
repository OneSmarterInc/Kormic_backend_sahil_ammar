"""Persist reset mail before delivery and retry transient SMTP failures."""
import logging
import math
import uuid
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from accounts.email import send_otp_email
from accounts.models import PasswordResetDelivery
from accounts.password_reset import OTP_TTL, hash_otp, is_current_otp, issue_otp

logger = logging.getLogger(__name__)


def delivery_code(user_id: int, nonce: uuid.UUID) -> str:
    digest = salted_hmac('kormic.password-reset.delivery', f'{user_id}:{nonce}', algorithm='sha256').hexdigest()
    return f'{int(digest, 16) % 10**6:06d}'


def request_reset_delivery(user) -> None:
    nonce = uuid.uuid4()
    otp = issue_otp(user.pk, code=delivery_code(user.pk, nonce))
    now = timezone.now()
    job = PasswordResetDelivery.objects.create(user=user, nonce=nonce, otp_hash=hash_otp(otp),
        expires_at=now + timedelta(seconds=OTP_TTL), available_at=now)
    # Preserve immediate delivery; a failed attempt remains durable for the worker.
    deliver(job.pk)


def deliver(job_id: int) -> None:
    now = timezone.now()
    pending = PasswordResetDelivery.objects.filter(pk=job_id, sent_at__isnull=True, available_at__lte=now)
    # Atomic lease prevents concurrent web/worker delivery of the same code.
    if not pending.update(available_at=now + timedelta(seconds=60), attempts=F('attempts') + 1):
        return
    job = PasswordResetDelivery.objects.select_related('user').get(pk=job_id)
    if job.expires_at <= now or not job.user.is_active or not is_current_otp(job.user_id, job.otp_hash):
        job.delete()
        return
    try:
        send_otp_email(job.user, delivery_code(job.user_id, job.nonce),
            ttl_minutes=max(1, math.ceil((job.expires_at - now).total_seconds() / 60)))
    except Exception as exc:
        # Exception class only: provider responses may contain recipient details.
        PasswordResetDelivery.objects.filter(pk=job.pk).update(last_error=type(exc).__name__[:100])
        logger.warning('password_reset_delivery_retry job=%s attempt=%s error=%s', job.pk, job.attempts, type(exc).__name__)
        return
    PasswordResetDelivery.objects.filter(pk=job.pk).update(sent_at=timezone.now(), last_error='')
    logger.info('password_reset_delivery_sent job=%s attempt=%s', job.pk, job.attempts)


@shared_task(ignore_result=True)
def retry_reset_deliveries() -> None:
    now = timezone.now()
    PasswordResetDelivery.objects.filter(expires_at__lte=now).delete()
    for pk in PasswordResetDelivery.objects.filter(sent_at__isnull=True, available_at__lte=now).order_by('available_at').values_list('pk', flat=True)[:20]:
        deliver(pk)
