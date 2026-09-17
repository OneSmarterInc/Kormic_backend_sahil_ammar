# institutes_list/tasks.py
# Background delivery for institute claim-flow emails. HTTP views do only
# the fast validation/state transition work; SMTP is isolated in Celery so a
# slow or temporarily unavailable mail provider cannot turn an API response
# into an HTML 500 page or hold a web worker open.
from __future__ import annotations

import hashlib
import logging

from celery import shared_task
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)

CLAIM_OTP_CACHE_PREFIX = "claim-otp-delivery"


def claim_otp_cache_key(listed_student_id: int, otp_hash: str) -> str:
    """Return the short-lived cache key used to hand an OTP to Celery.

    The database keeps only the SHA-256 hash. The plaintext code is held in
    the shared cache only until delivery succeeds (or the OTP expires), so it
    is not serialized into Celery task arguments or persisted in a model.
    """
    return f"{CLAIM_OTP_CACHE_PREFIX}:{listed_student_id}:{otp_hash}"


def cache_claim_otp_code(
    listed_student_id: int,
    otp_hash: str,
    code: str,
    *,
    timeout: int,
) -> bool:
    """Store and verify the short-lived plaintext delivery payload.

    Django's cache API does not guarantee that ``set`` returns a truthy
    value for every backend, so verify the write with a read instead of
    relying on a backend-specific return value.
    """
    key = claim_otp_cache_key(listed_student_id, otp_hash)
    cache.set(key, code, timeout=timeout)
    return cache.get(key) == code


def discard_claim_otp_code(listed_student_id: int, otp_hash: str) -> None:
    """Best-effort cleanup that must never turn a JSON API error into HTML."""
    try:
        cache.delete(claim_otp_cache_key(listed_student_id, otp_hash))
    except Exception:
        logger.exception(
            "Unable to remove cached claim OTP for ListedStudent %s.",
            listed_student_id,
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_invite_email_task(self, listed_student_id: int) -> None:
    from django.conf import settings
    from urllib.parse import urlencode

    from institutes_list.models import ListedStudent

    row = (
        ListedStudent.objects.select_related("source_list__institute")
        .filter(id=listed_student_id)
        .first()
    )
    if row is None:
        logger.warning("send_invite_email_task: ListedStudent %s no longer exists.", listed_student_id)
        return

    claim_link = f"{settings.CLAIM_PAGE_URL}?{urlencode({'token': row.claim_token})}"

    try:
        send_mail(
            subject="You're invited to claim your Kormic profile",
            message=(
                f"Hi {row.full_name},\n\n"
                f"{row.source_list.institute.name} has listed you for a Kormic profile. "
                f"Claim it here: {claim_link}\n\n"
                f"Already have the app open? Enter this token directly instead: {row.claim_token}\n\n"
                "This link/token identifies you but reveals nothing on its own -- "
                "you'll still need to verify your email with a one-time code."
            ),
            from_email=None,  # DEFAULT_FROM_EMAIL
            recipient_list=[row.email],
            fail_silently=False,
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def send_claim_otp_email_task(
    self,
    listed_student_id: int,
    expected_otp_hash: str,
) -> None:
    """Deliver the currently valid claim OTP for one invitation.

    A resend replaces ``ListedStudent.otp_hash``. Before every attempt this
    task compares the expected hash with the row, which makes delayed or
    retried tasks for an older code harmless. The plaintext code remains in
    the shared cache across transient SMTP retries and is deleted immediately
    after a successful send or when the task is stale.
    """
    from institutes_list.models import ListedStudent

    cache_key = claim_otp_cache_key(listed_student_id, expected_otp_hash)

    try:
        code = cache.get(cache_key)
    except Exception as exc:
        logger.exception(
            "send_claim_otp_email_task: cache read failed for ListedStudent %s.",
            listed_student_id,
        )
        raise self.retry(exc=exc)

    if not isinstance(code, str) or not code:
        logger.warning(
            "send_claim_otp_email_task: OTP delivery payload is unavailable for ListedStudent %s.",
            listed_student_id,
        )
        return

    if hashlib.sha256(code.encode("utf-8")).hexdigest() != expected_otp_hash:
        logger.error(
            "send_claim_otp_email_task: cached OTP integrity check failed for ListedStudent %s.",
            listed_student_id,
        )
        discard_claim_otp_code(listed_student_id, expected_otp_hash)
        return

    try:
        row = ListedStudent.objects.filter(id=listed_student_id).first()
    except Exception as exc:
        logger.exception(
            "send_claim_otp_email_task: database read failed for ListedStudent %s.",
            listed_student_id,
        )
        raise self.retry(exc=exc)

    is_current = bool(
        row
        and row.status == ListedStudent.Status.UNCLAIMED
        and row.otp_hash == expected_otp_hash
        and row.otp_expires_at
        and row.otp_expires_at > timezone.now()
    )
    if not is_current:
        logger.info(
            "send_claim_otp_email_task: skipping stale OTP delivery for ListedStudent %s.",
            listed_student_id,
        )
        discard_claim_otp_code(listed_student_id, expected_otp_hash)
        return

    try:
        send_mail(
            subject="Your Kormic claim code",
            message=(
                f"Your one-time code is {code}. It expires in 10 minutes.\n\n"
                "Your institute listed this address so you can claim your Kormic "
                "profile. If you did not request this, you can ignore it."
            ),
            from_email=None,  # DEFAULT_FROM_EMAIL
            recipient_list=[row.email],
            fail_silently=False,
        )
    except Exception as exc:
        # Keep the cache entry for the retry. A newer resend changes the row
        # hash, so a retry of this task will safely self-cancel as stale.
        raise self.retry(exc=exc)

    # Do not retry a successfully delivered email only because cache cleanup
    # had a transient problem; the cache entry has the same short TTL as the
    # OTP and cannot be used after the row expires or is replaced.
    discard_claim_otp_code(listed_student_id, expected_otp_hash)
