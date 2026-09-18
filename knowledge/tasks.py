from celery import shared_task
from django.utils import timezone
from django.db.models import Q

@shared_task(soft_time_limit=240, time_limit=270, ignore_result=True)
def refresh_knowledge():
    from knowledge.freshness import track_sources, recrawl
    from django_api.models import KnowledgeSource
    track_sources()
    ids = KnowledgeSource.objects.filter(enabled=True, next_fetch_at__lte=timezone.now()).filter(Q(lease_until__isnull=True) | Q(lease_until__lt=timezone.now())).order_by("next_fetch_at").values_list("pk", flat=True)[:10]
    for pk in ids: recrawl(pk)

@shared_task(soft_time_limit=60, time_limit=90, ignore_result=True)
def embed_knowledge():
    from knowledge.retrieval import index_pending
    return index_pending()
