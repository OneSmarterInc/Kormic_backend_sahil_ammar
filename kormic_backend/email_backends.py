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
    """Real recipients use real SMTP as the delivery authority.

    Ethereal is a best-effort QA copy after a real send. For fake recipients it
    remains the only destination. A QA-copy failure never replays a real email.
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
        from copy import copy
        real_message = copy(message)
        real_message.to = [a for a in message.to if not _is_fake_recipient(a)]
        real_message.cc = [a for a in message.cc if not _is_fake_recipient(a)]
        real_message.bcc = [a for a in message.bcc if not _is_fake_recipient(a)]
        has_real_recipients = bool(real_message.recipients())
        if has_real_recipients:
            try:
                if self._real.send_messages([real_message]) != 1:
                    raise RuntimeError("The real SMTP provider did not accept this message.")
            except Exception:
                if self.fail_silently:
                    logger.exception("Real SMTP delivery failed; message was not sent.")
                    return False
                raise
        try:
            copied = self._ethereal.send_messages([message]) == 1
        except Exception:
            if not has_real_recipients and not self.fail_silently:
                raise
            logger.warning("Sandbox email copy failed; real SMTP acceptance is unchanged.", exc_info=True)
            copied = False
        return has_real_recipients or copied
