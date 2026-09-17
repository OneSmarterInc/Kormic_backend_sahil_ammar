from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_otp_email(user: User, otp: str, *, ttl_minutes: int) -> None:
    subject = "Your Kormic Commons password reset code"
    message = (
        "Hi,\n\n"
        f"Your one-time password reset code is: {otp}\n\n"
        f"This code expires in {ttl_minutes} minutes and can only be used once.\n\n"
        "If you did not request a password reset, you can safely ignore this "
        "email -- your password will not be changed.\n\n"
        "-- Kormic Commons"
    )
    _send(subject, message, user.email)


def send_password_changed_email(user: User) -> None:
    subject = "Your Kormic Commons password was changed"
    message = (
        "Hi,\n\n"
        "This is a confirmation that your password was just changed. All of "
        "your active sessions have been signed out and you'll need to log "
        "in again with the new password.\n\n"
        "If you did not make this change, please contact support immediately.\n\n"
        "-- Kormic Commons"
    )
    _send(subject, message, user.email)


def _send(subject: str, message: str, to_email: str) -> None:
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
    except Exception:
        # Never let an email-provider outage 500 the request, and never let
        # the caller distinguish "sent" from "failed to send" -- that alone
        # could confirm whether an account exists. Log for ops and move on.
        logger.exception("Failed to send email to %s", to_email)
