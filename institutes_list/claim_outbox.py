"""Claim OTP delivery for installations using the database invitation worker."""
from datetime import timedelta
import logging

from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import ClaimCodeDelivery
from .tasks import cache_claim_otp_code, discard_claim_otp_code, send_claim_otp_email_task

logger = logging.getLogger(__name__)


def delivery_code(student_id, nonce):
    digest = salted_hmac("kormic.claim.delivery", f"{student_id}:{nonce}", algorithm="sha256").hexdigest()
    return f"{int(digest, 16) % 10**6:06d}"


def work_once():
    now = timezone.now()
    ClaimCodeDelivery.objects.filter(expires_at__lte=now).delete()
    eligible = ClaimCodeDelivery.objects.filter(available_at__lte=now)
    job = eligible.order_by("available_at", "id").first()
    if job is None or not eligible.filter(pk=job.pk).update(available_at=now + timedelta(minutes=1)):
        return False

    row = job.student
    if (row.status != "unclaimed" or row.source_list.status != "active"
            or row.otp_hash != job.otp_hash):
        job.delete()
        return True

    # The worker and API can use separate process-local caches. Recreate the
    # short-lived task payload here, only for the duration of delivery.
    try:
        if not cache_claim_otp_code(row.id, job.otp_hash, delivery_code(row.id, job.nonce),
                                   timeout=max(1, int((job.expires_at - now).total_seconds()))):
            return True
        result = send_claim_otp_email_task.apply(args=[row.id, job.otp_hash], throw=False)
        if result.successful():
            job.delete()
    except Exception:
        logger.exception("Claim code delivery failed for outbox job %s; retrying after its lease expires.", job.pk)
    finally:
        discard_claim_otp_code(row.id, job.otp_hash)
    return True
