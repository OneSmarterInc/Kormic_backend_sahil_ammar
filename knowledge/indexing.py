"""Transactional ingestion outbox. Coalesces edits without dropping late writes."""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db.models import F
from django_api.models import UniversityKnowledgeEntry, KnowledgeIndexWork


@receiver(post_save, sender=UniversityKnowledgeEntry)
@receiver(post_delete, sender=UniversityKnowledgeEntry)
def changed(sender, instance, **kwargs):
    # This writes in the SAME transaction as the fact. Broker downtime cannot
    # lose an indexing request. Raw fixture loads are not ingestion.
    if kwargs.get("raw"):
        return
    fields = kwargs.get("update_fields")
    if fields and not set(fields) & {"topic", "content", "details", "confidence", "source_type", "source_url", "group", "group_id"}:
        return
    if kwargs.get("signal") is post_save:
        UniversityKnowledgeEntry.objects.filter(pk=instance.pk).update(embedding=None, embedding_hash="", embedding_model="")
    work, _ = KnowledgeIndexWork.objects.get_or_create(university_id=instance.university_id)
    KnowledgeIndexWork.objects.filter(pk=work.pk).update(revision=F("revision") + 1)
