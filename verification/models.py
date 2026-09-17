from __future__ import annotations

from django.db import models

from django_api.models import StudentProfile


class VerificationCheck(models.Model):
    """
    One row per student -- the current verification cycle. Recomputed in
    place by verification.services.run_verification() on every status/
    reanalyze call; the VerificationItem rows underneath it are reconciled
    against the fresh analysis rather than replaced wholesale, so a
    student's confirm/ignore/clarify on one item survives an unrelated
    reanalysis (e.g. reuploading GitHub doesn't wipe out an already-resolved
    email mismatch flagged on the resume).
    """

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"        # a required source hasn't been uploaded yet
        NEEDS_REVIEW = "needs_review", "Needs review"    # all sources present, unresolved items remain
        VERIFIED = "verified", "Verified"                # all sources present, every item resolved (or none raised)
        ERROR = "error", "Error"                          # the analysis itself threw

    class Engine(models.TextChoices):
        AI = "ai", "AI holistic judge"
        RULE_FALLBACK = "rule_fallback", "Deterministic fallback"

    student = models.OneToOneField(StudentProfile, on_delete=models.CASCADE, related_name="verification_check")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INCOMPLETE)
    missing_sources = models.JSONField(default=list, blank=True)
    last_error = models.TextField(blank=True, default="")
    # Which engine actually produced the current items -- "ai" normally,
    # "rule_fallback" only when the LLM call failed. Surfaced to the
    # frontend/admin so a fallback run is visibly distinguishable from a
    # full AI analysis rather than silently passing as equivalent.
    engine = models.CharField(max_length=20, choices=Engine.choices, blank=True, default="")
    last_analyzed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"VerificationCheck({self.student.uuid}, {self.status})"


class VerificationItem(models.Model):
    """
    One flagged question the student needs to act on -- a single
    disagreement surfaced by the verification engine, possibly spanning
    more than one source (e.g. "resume and LinkedIn disagree on
    institution"). Rows are never deleted, only marked resolved, so the
    full history of what was flagged and how the student responded stays
    auditable.
    """

    class Severity(models.TextChoices):
        MODERATE = "moderate", "Moderate"
        HIGH = "high", "High"

    class Resolution(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed by student"
        IGNORED = "ignored", "Ignored by student"
        CLARIFIED = "clarified", "Clarified by student"
        AUTO_CLEARED = "auto_cleared", "Auto-cleared on reanalysis"
        SUPERSEDED = "superseded", "Superseded by new upload"

    verification_check = models.ForeignKey(VerificationCheck, on_delete=models.CASCADE, related_name="items")

    # Stable identity for "the same underlying disagreement" across
    # reanalyses -- "<dimension>:<sorted sources joined by +>",
    # e.g. "institution:linkedin+resume".
    key = models.CharField(max_length=150, db_index=True)
    dimension = models.CharField(max_length=50)
    sources = models.JSONField(default=list, blank=True)
    severity = models.CharField(max_length=20, choices=Severity.choices, default=Severity.MODERATE)
    confidence = models.FloatField(null=True, blank=True)

    expected_value = models.TextField(blank=True, default="")
    found_value = models.TextField(blank=True, default="")
    message = models.TextField()

    source_signature = models.JSONField(default=dict, blank=True)

    is_resolved = models.BooleanField(default=False, db_index=True)
    resolution = models.CharField(max_length=20, choices=Resolution.choices, blank=True, default="")
    student_note = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["is_resolved", "-created_at"]

    def __str__(self) -> str:
        return f"VerificationItem({self.key}, resolved={self.is_resolved})"
