from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import StudentFaceChallenge


class Command(BaseCommand):
    help = 'Clear expired temporary face templates and remove challenge metadata older than 30 days.'

    def handle(self, *args, **options):
        now = timezone.now()
        expired = StudentFaceChallenge.objects.filter(expires_at__lte=now, finished_at__isnull=True)
        cleared = expired.update(encrypted_reference=b'', finished_at=now)
        # Keep successful nonce records at least as long as pending JWT validity.
        removed, _ = StudentFaceChallenge.objects.filter(created_at__lt=now - timedelta(days=30)).delete()
        self.stdout.write(f'Cleared {cleared} expired face challenges; removed {removed} old records.')
