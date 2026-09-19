"""Tenant-scoped source health and crawl controls."""
import os
from django.utils import timezone
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import IsTOTPEnrolled, IsUniversityRole
from django_api.models import KnowledgeSource, KnowledgeChunk
from knowledge.retrieval import MODEL

class KnowledgeSourcesView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]
    def get(self, request):
        sources = KnowledgeSource.objects.filter(university=request.user.account.university)
        chunks = KnowledgeChunk.objects.filter(entry__university_id=request.user.account.university_uuid, entry__active=True)
        return Response({"sources": list(sources.values("id", "url", "health", "enabled", "content_hash", "last_fetched_at", "last_success_at", "changed_at", "next_fetch_at", "stale_days", "recrawl_hours", "http_status", "failures")),
            "embedding": {"configured": bool(os.getenv("VOYAGE_API_KEY")), "model": MODEL, "chunks": chunks.count(), "pending": chunks.filter(Q(embedding__isnull=True) | ~Q(embedding_model=MODEL)).count()}})
    def patch(self, request):
        from django.shortcuts import get_object_or_404
        source = get_object_or_404(KnowledgeSource, pk=request.data.get("id"), university=request.user.account.university)
        if set(request.data) - {"id", "enabled", "stale_days", "recrawl_hours", "recrawl"}: raise ValidationError("Unknown source setting.")
        for field, maximum in [("stale_days", 365), ("recrawl_hours", 720)]:
            if field in request.data:
                value = request.data[field]
                if type(value) is not int or not 1 <= value <= maximum: raise ValidationError({field: [f"Enter a whole number from 1 to {maximum}."]})
                setattr(source, field, value)
        if "enabled" in request.data:
            if type(request.data["enabled"]) is not bool: raise ValidationError("enabled must be a boolean.")
            source.enabled = request.data["enabled"]
        if request.data.get("recrawl") is True: source.next_fetch_at = timezone.now()
        source.save(update_fields=["enabled", "stale_days", "recrawl_hours", "next_fetch_at"])
        return self.get(request)
