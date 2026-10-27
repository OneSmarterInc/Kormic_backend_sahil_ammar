"""Bounded SMTP delivery from a durable outbox shared by both worker modes."""
import logging
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.utils import timezone
from .models import EmailDelivery

logger = logging.getLogger(__name__)


@transaction.atomic
def work_once() -> bool:
    row = EmailDelivery.objects.select_for_update(skip_locked=True).filter(
        sent_at__isnull=True, attempts__lt=5, available_at__lte=timezone.now(),
    ).order_by('available_at', 'pk').first()
    if row is None:
        return False
    log = row.notification
    user = log.account.user
    if not user.is_active or not user.email:
        row.attempts = 5
        row.last_error = 'recipient_unavailable'
        row.save(update_fields=['attempts', 'last_error'])
        return True
    origin = getattr(settings, 'NOTIFICATION_PORTAL_URL', 'https://platform.kormic.ai').rstrip('/')
    route = str(log.data.get('route', ''))
    if log.account.role == 'student':
        route = '/student/'
    if not route.startswith('/') or route.startswith('//') or urlsplit(route).netloc:
        route = '/'
    portal = {'superuser': 'superuser', 'university': 'university', 'department': 'university', 'institute': 'institute'}.get(log.account.role)
    if portal:
        route = f'/{portal}/#{route}'
    body = f'{log.body}\n\nOpen Kormic to view the details and respond:\n{origin}{route}\n\nPlease respond in Kormic, not by replying to this email.'
    row.attempts += 1
    try:
        with get_connection(timeout=5) as connection:
            count = EmailMessage(log.title, body, settings.DEFAULT_FROM_EMAIL, [user.email],
                connection=connection, headers={'Message-ID': f'<notification-{log.pk}@kormic.ai>'}).send()
        if count != 1:
            raise RuntimeError('SMTP did not accept the notification')
        row.sent_at = timezone.now()
        row.last_error = ''
    except Exception as exc:
        row.last_error = type(exc).__name__[:100]
        row.available_at = timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** row.attempts))
        logger.warning('Notification email delivery failed id=%s error=%s', row.pk, row.last_error)
    row.save(update_fields=['attempts', 'sent_at', 'last_error', 'available_at'])
    return True
