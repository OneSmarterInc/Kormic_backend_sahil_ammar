"""Tasks imported by Celery autodiscovery."""
from celery import shared_task

from accounts.reset_delivery import request_password_reset, retry_reset_deliveries  # noqa: F401


@shared_task(ignore_result=True)
def cleanup_face_challenges():
    from django.core.management import call_command
    call_command('cleanup_face_challenges')
