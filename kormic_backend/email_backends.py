from __future__ import annotations

import logging
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend

logger = logging.getLogger(__name__)


def _is_fake_recipient(address: str) -> bool:
    _, addr = parseaddr(address)
    domain = addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""
    return domain in settings.FAKE_EMAIL_DOMAINS


class DualSendEmailBackend(BaseEmailBackend):
    """
    UAT mode sends every email to Ethereal for QA verification.
    Real SMTP sending is also attempted for recipients not using seeded/test domains listed in FAKE_EMAIL_DOMAINS.
    This allows testing real-world deliverability, such as spam filtering, without sending emails to known placeholder addresses.
    Real SMTP failures are logged and ignored because fake or invalid addresses may bounce.
    Ethereal is the authoritative send, so only Ethereal failures are treated as actual email failures and respect Django's fail_silently behavior.
    """

    def __init__(self, fail_silently: bool = False, **kwargs) -> None:
        super().__init__(fail_silently=fail_silently)
        self._real = SMTPEmailBackend(
            host=settings.EMAIL_HOST,
            port=settings.EMAIL_PORT,
            username=settings.EMAIL_HOST_USER,
            password=settings.EMAIL_HOST_PASSWORD,
            use_tls=settings.EMAIL_USE_TLS,
            use_ssl=settings.EMAIL_USE_SSL,
            # Always False here regardless of the caller's fail_silently --
            # Django's own SMTP backend swallows most SMTP errors internally
            # when its own fail_silently is True, which would hide a real
            # misconfiguration (e.g. a bad password) from the try/except
            # below just as completely as an expected fake-address bounce.
            # We need the exception to reach _send_one so it always gets
            # logged, even though it's then deliberately not re-raised.
            fail_silently=False,
        )
        self._ethereal = SMTPEmailBackend(
            host=settings.ETHEREAL_HOST,
            port=settings.ETHEREAL_PORT,
            username=settings.ETHEREAL_HOST_USER,
            password=settings.ETHEREAL_HOST_PASSWORD,
            use_tls=settings.ETHEREAL_USE_TLS,
            use_ssl=settings.ETHEREAL_USE_SSL,
            fail_silently=fail_silently,
        )

    def send_messages(self, email_messages) -> int:
        if not email_messages:
            return 0
        return sum(1 for message in email_messages if self._send_one(message))

    def _send_one(self, message) -> bool:
        real_recipients = [a for a in message.to if not _is_fake_recipient(a)]
        if real_recipients:
            real_message = message
            original = (real_message.to, real_message.cc, real_message.bcc)
            real_message.to = real_recipients
            real_message.cc = [a for a in message.cc if not _is_fake_recipient(a)]
            real_message.bcc = [a for a in message.bcc if not _is_fake_recipient(a)]
            try:
                self._real.send_messages([real_message])
            except Exception:
                logger.warning(
                    "Dual-send: real-provider send failed for %r (recipient likely fake but "
                    "not on FAKE_EMAIL_DOMAINS) -- Ethereal copy still sent",
                    message.subject,
                    exc_info=True,
                )
            finally:
                real_message.to, real_message.cc, real_message.bcc = original

        # Ethereal always gets the untouched, original recipient list -- it's
        # the audit copy of exactly what was attempted.
        return self._ethereal.send_messages([message]) == 1
