from __future__ import annotations

from django.db import models


class DiscoveryJob(models.Model):
    """One crawl run of a university's public website to find candidate
    knowledge-base pages. Field names mirror the university-admin
    scrape_urls flow: results here are only ever *proposed* -- an officer
    still chooses what actually lands in University.scrape_urls."""

    ACTIVE_STATUSES = ("queued", "running", "stop_requested")

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        STOP_REQUESTED = "stop_requested", "Stop requested"
        STOPPED = "stopped", "Stopped"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    university = models.ForeignKey(
        "universities.University", on_delete=models.CASCADE, related_name="discovery_jobs"
    )
    base_url = models.TextField()
    root_domain = models.CharField(max_length=255, blank=True, default="")
    allowed_domains = models.JSONField(default=list, blank=True)
    settings = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=40, choices=Status.choices, default=Status.QUEUED, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    # How many curated candidate URLs to keep, and whether to automatically
    # apply them to University.scrape_urls and run scrape_now once the crawl
    # completes -- the fully-automatic "just get me a knowledge base" path.
    # An officer can still review DiscoveredUrl rows manually and call
    # apply/scrape-now themselves regardless of these settings.
    top_n = models.IntegerField(default=20)
    auto_apply = models.BooleanField(default=True)
    scrape_result = models.JSONField(default=dict, blank=True)

    pages_discovered = models.IntegerField(default=0)
    pages_crawled = models.IntegerField(default=0)
    relevant_count = models.IntegerField(default=0)
    review_count = models.IntegerField(default=0)
    excluded_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    current_url = models.TextField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"DiscoveryJob({self.id}, {self.university_id}, {self.status})"


class DiscoveredUrl(models.Model):
    """One page found while crawling a DiscoveryJob's base_url. Field names
    intentionally mirror the original auto_university_urls_parser UrlRecord
    schema so classifier.as_storage() / important_urls selection logic can
    be reused without modification."""

    job = models.ForeignKey(DiscoveryJob, on_delete=models.CASCADE, related_name="urls")

    original_url = models.TextField()
    normalized_url = models.TextField()
    final_url = models.TextField(null=True, blank=True)
    parent_url = models.TextField(null=True, blank=True)
    anchor_text = models.CharField(max_length=500, null=True, blank=True)
    page_title = models.TextField(null=True, blank=True)
    meta_description = models.TextField(null=True, blank=True)
    h1 = models.TextField(null=True, blank=True)
    content_type = models.CharField(max_length=255, null=True, blank=True)
    file_extension = models.CharField(max_length=32, null=True, blank=True)
    http_status = models.IntegerField(null=True, blank=True)
    crawl_depth = models.IntegerField(default=0)

    primary_category = models.CharField(max_length=255, null=True, blank=True)
    secondary_categories_json = models.TextField(default="[]")
    category_scores_json = models.TextField(default="{}")
    relevance_score = models.FloatField(default=0.0)
    confidence = models.FloatField(default=0.0)
    decision_status = models.CharField(max_length=40, default="pending", db_index=True)
    is_discovery_only = models.BooleanField(default=True)
    classification_reason = models.TextField(null=True, blank=True)
    exclusion_reason = models.TextField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    discovered_at = models.DateTimeField(auto_now_add=True)
    crawled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "normalized_url"], name="uq_discovery_job_normalized_url"),
        ]
        indexes = [
            models.Index(fields=["job", "decision_status"], name="ix_durl_job_status"),
            models.Index(fields=["job", "primary_category"], name="ix_durl_job_category"),
        ]

    def __str__(self) -> str:
        return f"DiscoveredUrl({self.normalized_url})"


class DiscoveryClusterApproval(models.Model):
    """Provenance for the human review step: one row per approved
    (job, category) cluster -- who approved it and when, plus a snapshot of
    the URLs that were applied at that moment. `category` is the
    DiscoveredUrl.primary_category value the cluster was grouped by;
    `knowledge_group` is the resolved mapping (url_discovery.group_mapping)
    the cluster's scraped facts were tagged with."""

    job = models.ForeignKey(DiscoveryJob, on_delete=models.CASCADE, related_name="cluster_approvals")
    category = models.CharField(max_length=255)
    knowledge_group = models.ForeignKey(
        "universities.KnowledgeGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discovery_cluster_approvals",
    )
    url_count = models.IntegerField(default=0)
    urls = models.JSONField(default=list, blank=True)
    approved_by = models.CharField(max_length=255, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-approved_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "category"], name="uq_discovery_cluster_job_category"),
        ]

    def __str__(self) -> str:
        return f"DiscoveryClusterApproval(job={self.job_id}, category={self.category})"
