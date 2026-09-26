"""Drain locally queued invitation emails without blocking institute requests."""
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from institutes_list.models import ListedStudent
from institutes_list.tasks import send_invite_email_task


def work_once():
    now = timezone.now()
    # A crashed worker's lease expires; SMTP timeout is 15 seconds, well below this.
    eligible = ListedStudent.objects.filter(invite_delivery_status="queued").filter(
        Q(invite_delivery_started_at__isnull=True) |
        Q(invite_delivery_started_at__lt=now - timedelta(minutes=5)))
    row_id = eligible.order_by("invited_at", "id").values_list("id", flat=True).first()
    if row_id is None or not eligible.filter(id=row_id).update(invite_delivery_started_at=now):
        return False
    # Local SMTP failures remain visible and can be explicitly retried in the roster.
    # Celery's eager context prevents publishing retries to an absent local broker.
    send_invite_email_task.apply(args=[row_id], throw=False)
    return True


class Command(BaseCommand):
    help = "Deliver the persistent invitation outbox (INVITE_DELIVERY_MODE=database)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if settings.INVITE_DELIVERY_MODE != "database":
            self.stdout.write("Invitation delivery uses Celery; no database worker is required.")
            return
        while True:
            close_old_connections()
            worked = work_once()
            if options["once"]:
                return
            if not worked:
                time.sleep(2)
