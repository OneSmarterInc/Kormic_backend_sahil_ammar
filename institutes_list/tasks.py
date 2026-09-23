# institutes_list/tasks.py
# Background delivery for institute claim-flow emails. HTTP views do only
# the fast validation/state transition work; SMTP is isolated in Celery so a
# slow or temporarily unavailable mail provider cannot turn an API response
# into an HTML 500 page or hold a web worker open.
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.core.cache import cache
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.utils.crypto import constant_time_compare

logger = logging.getLogger(__name__)

CLAIM_OTP_CACHE_PREFIX = "claim-otp-delivery"


@shared_task
def purge_expired_source_files() -> dict[str, int]:
    """Delete raw roster files after the configured retention window."""
    from institutes_list.models import InstituteStudentList
    from institutes_list.source_files import resolve_source_file_path

    retention_days = max(
        1, int(getattr(settings, "INSTITUTE_ROSTER_SOURCE_RETENTION_DAYS", 30))
    )
    cutoff = timezone.now() - timedelta(days=retention_days)
    results = {"deleted": 0, "missing": 0, "unsafe": 0, "failed": 0}

    expired = InstituteStudentList.objects.filter(
        created_at__lt=cutoff,
    ).exclude(source_file_path="")
    for source_list in expired.iterator():
        try:
            file_path = resolve_source_file_path(source_list.source_file_path)
        except ValueError:
            logger.error(
                "Refusing to delete roster source path outside MEDIA_ROOT for list %s.",
                source_list.id,
            )
            results["unsafe"] += 1
            continue

        try:
            if file_path.exists():
                file_path.unlink()
                results["deleted"] += 1
            else:
                results["missing"] += 1
        except OSError:
            logger.exception("Unable to delete roster source file for list %s.", source_list.id)
            results["failed"] += 1
            continue

        source_list.source_file_path = ""
        source_list.source_file_name = ""
        source_list.source_file_content_type = ""
        source_list.source_file_size = 0
        source_list.save(
            update_fields=[
                "source_file_path",
                "source_file_name",
                "source_file_content_type",
                "source_file_size",
            ]
        )

    return results


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
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[row.email],
            fail_silently=False,
        )
    except Exception as exc:
        error_text = str(exc)[:500]
        logger.exception("Invite email failed for ListedStudent %s", listed_student_id)
        ListedStudent.objects.filter(id=listed_student_id).update(
            invite_delivery_status="failed",
            invite_delivery_error=error_text,
        )
        raise self.retry(exc=exc)

    ListedStudent.objects.filter(id=listed_student_id).update(
        invite_delivery_status="sent",
        invite_delivery_error="",
        invite_delivered_at=timezone.now(),
    )


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

    from institutes_list.views import _hash_otp

    if not constant_time_compare(
        _hash_otp(listed_student_id, code),
        expected_otp_hash,
    ):
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
                f"{row.source_list.institute.name} listed this address so you can claim your Kormic "
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
