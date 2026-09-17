
from __future__ import annotations

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def default_priority_tier_bounds() -> dict:
    return {"high": 80, "medium": 60, "low": 40}


class University(models.Model):
    # Public, non-guessable identifier used in every API URL, cache key, and
    # cross-table string reference. The integer auto `id` stays purely
    # internal (FKs, joins, admin). Replaces the previous name-derived slug PK.
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    # -- identity (mirrors the old UNIVERSITY_PERSONAS entry shape) --
    name = models.CharField(max_length=500)
    agent_name = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    location = models.CharField(max_length=255, blank=True, default="")
    tagline = models.CharField(max_length=500, blank=True, default="")

    # -- setup-phase structured fields, admin-editable any time --
    description = models.TextField(blank=True, default="")

    contact_email = models.CharField(max_length=255, blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")
    website_url = models.CharField(max_length=1000, blank=True, default="")
    admissions_office_address = models.TextField(blank=True, default="")

    # [{"criterion": "...", "detail": "..."}]
    eligibility_criteria = models.JSONField(default=list, blank=True)
    # ["https://...", ...] -- same flat shape knowledge.scraper.scrape_university() expects
    scrape_urls = models.JSONField(default=list, blank=True)

    min_fit_score_threshold = models.IntegerField(
        default=40,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    priority_tier_bounds = models.JSONField(default=default_priority_tier_bounds, blank=True)

    # -- persona-input fields feeding personas.university_persona_builder --
    tone_descriptors = models.JSONField(default=list, blank=True)
    best_fit_notes = models.TextField(blank=True, default="")
    not_best_fit_notes = models.TextField(blank=True, default="")
    communication_style_notes = models.TextField(blank=True, default="")
    never_do_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"University({self.id}, {self.name})"


class KnowledgeGroup(models.Model):

    class Slug(models.TextChoices):
        ADMISSIONS = "admissions", "Admissions"
        INTERNATIONAL = "international", "International Office"
        MONEY = "money", "Money"
        CAMPUS_LIFE = "campus_life", "Campus Life"

    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name="knowledge_groups")
    slug = models.CharField(max_length=30, choices=Slug.choices)
    escalation_contact_name = models.CharField(max_length=255, blank=True, default="")
    escalation_contact_email = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["university_id", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["university", "slug"], name="uq_knowledge_group_university_slug"),
        ]

    def __str__(self) -> str:
        return f"KnowledgeGroup({self.university_id}, {self.slug})"


class ScrapeJob(models.Model):
    """
    Mirrors url_discovery.DiscoveryJob's
    queued/running/completed/failed shape, deliberately smaller since a
    scrape run has no per-page progress worth tracking, just a final result.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    ACTIVE_STATUSES = ("queued", "running")

    university = models.ForeignKey(University, on_delete=models.CASCADE, related_name="scrape_jobs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    result = models.JSONField(default=dict, blank=True)
    error_message = models.CharField(max_length=1000, blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ScrapeJob({self.university_id}, {self.status})"
