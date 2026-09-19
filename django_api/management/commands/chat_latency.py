"""Aggregate durable job latency; no high-cardinality labels or user data."""
import math
from django.core.management.base import BaseCommand
from django_api.models import ChatGeneration
from django_api.chat_jobs import expire_jobs


class Command(BaseCommand):
    help = "Report p50/p95/p99 end-to-end chat latency and timeout/error counts."

    def handle(self, *args, **options):
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Count
        expire_jobs()
        qs = ChatGeneration.objects.filter(created_at__gte=timezone.now() - timedelta(days=1))
        values = sorted(qs.exclude(latency_ms=None).values_list("latency_ms", flat=True))
        self.stdout.write(str({"window": "24h", "sample_count": len(values),
            **{f"p{p}_ms": values[max(0, math.ceil(len(values) * p / 100) - 1)] if values else None for p in (50, 95, 99)},
            "outcomes": list(qs.values("status", "error_code").annotate(count=Count("pk")))}))
