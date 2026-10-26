from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from security_reports.models import CSPReport


class Command(BaseCommand):
    help = "Show daily CSP counters. No reports is not proof of a working collector."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)

    def handle(self, *args, **options):
        days = options["days"]
        if not 1 <= days <= 365:
            raise CommandError("days must be between 1 and 365")
        rows = CSPReport.objects.filter(
            day__gte=timezone.now().date() - timedelta(days=days - 1)
        ).order_by("day", "directive", "blocked_origin")
        for row in rows.iterator():
            self.stdout.write(f"{row.day} {row.directive} {row.blocked_origin} {row.count}")
        self.stdout.write("Reports are untrusted telemetry. Enforcement requires explicit approval.")
