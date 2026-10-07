"""Maintenance of short-lived public collection coordination records."""

from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .models import PublicCollectionJob


@shared_task(name='university_research.tasks.cleanup_collection_jobs')
def cleanup_collection_jobs():
    # These rows hold only a public scope and lease state, never the student's
    # question, conversation, answer, or source evidence.
    cutoff = timezone.now() - timedelta(days=7)
    deleted, _ = PublicCollectionJob.objects.filter(
        Q(status__in=['completed', 'failed'], updated_at__lt=cutoff) |
        Q(status='running', lease_expires_at__lt=cutoff)
    ).delete()
    return deleted
