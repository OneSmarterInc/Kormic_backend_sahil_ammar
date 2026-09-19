"""Reliable public entry point for the institute invitation claim flow.

The original claim/start implementation sent SMTP mail synchronously inside
its anonymous HTTP request. A mail timeout or provider error therefore
escaped through Django as an HTML 500 page; the mobile app then attempted to
parse that page as JSON and displayed ``Unexpected character: <``.

This module keeps the existing claim lookup/security rules but queues OTP
mail through Celery, the same delivery boundary already used by institute
invitation emails. It always returns a DRF JSON response, including when the
cache or broker is unavailable.
"""
from __future__ import annotations

import logging
import secrets

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import ListedStudent, UniversityStudentList
from .tasks import (
    cache_claim_otp_code,
    discard_claim_otp_code,
    send_claim_otp_email_task,
)
from .throttling import ClaimStartEmailThrottle, ClaimStartIPThrottle
from .views import OTP_TTL_SECONDS, _find_claimable, _hash_otp

logger = logging.getLogger(__name__)


def _accepted():
    # Identical for known, unknown, consumed and delivery-failure cases.
    # Never derive this placeholder from the stored address or invitation.
    return Response({"masked_email": "•••••@•••••", "message":
        "If an eligible invitation exists, a code will be sent. Check your email."})


class ClaimInvitationUnavailable(Exception):
    """Raised when an invitation is consumed while claim/start is running."""


def _clear_failed_otp_state(listed_student_id: int, expected_otp_hash: str) -> None:
    """Clear only the OTP generation that failed to queue.

    The hash predicate prevents an older failing request from erasing a newer
    resend that completed concurrently.
    """
    ListedStudent.objects.filter(
        id=listed_student_id,
        otp_hash=expected_otp_hash,
    ).update(otp_hash="", otp_expires_at=None, otp_attempts=0)
    discard_claim_otp_code(listed_student_id, expected_otp_hash)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ClaimStartIPThrottle, ClaimStartEmailThrottle])
def start_claim(request):
    """Queue a one-time verification code for a claimable invitation.

    Status and body never confirm roster membership. Delivery failures are
    recorded internally, without creating an availability-dependent oracle.
    """
    row = _find_claimable(
        email=str(request.data.get("email") or ""),
        token=str(request.data.get("token") or ""),
    )
    if not row:
        return _accepted()

    code = f"{secrets.randbelow(10**6):06d}"
    otp_hash = _hash_otp(code)
    expires_at = timezone.now() + timezone.timedelta(seconds=OTP_TTL_SECONDS)

    # Keep the database state and short-lived delivery payload aligned. No
    # plaintext OTP is stored in the database or serialized into task args.
    try:
        with transaction.atomic():
            locked = ListedStudent.objects.select_for_update().get(pk=row.pk)
            previous_hash = locked.otp_hash
            updated = ListedStudent.objects.filter(
                id=row.id,
                status=ListedStudent.Status.UNCLAIMED,
                source_list__status=UniversityStudentList.Status.ACTIVE,
            ).update(
                otp_hash=otp_hash,
                otp_expires_at=expires_at,
                otp_attempts=0,
            )
            if updated != 1:
                raise ClaimInvitationUnavailable()

            if not cache_claim_otp_code(
                row.id,
                otp_hash,
                code,
                timeout=OTP_TTL_SECONDS,
            ):
                raise RuntimeError("The OTP delivery cache did not accept the verification code.")
            if previous_hash:
                discard_claim_otp_code(row.id, previous_hash)
    except ClaimInvitationUnavailable:
        discard_claim_otp_code(row.id, otp_hash)
        return _accepted()
    except Exception:
        logger.exception("Unable to prepare claim OTP delivery for ListedStudent %s.", row.id)
        _clear_failed_otp_state(row.id, otp_hash)
        return _accepted()

    try:
        send_claim_otp_email_task.delay(row.id, otp_hash)
    except Exception:
        logger.exception("Unable to queue claim OTP delivery for ListedStudent %s.", row.id)
        _clear_failed_otp_state(row.id, otp_hash)
        return _accepted()

    return _accepted()
