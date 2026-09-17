from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone

from url_discovery.domain_policy import validate_public_base_url
from url_discovery.models import DiscoveredUrl, DiscoveryClusterApproval, DiscoveryJob
from url_discovery.url_normalizer import normalize_url

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 150
MIN_MAX_PAGES = 20
MAX_MAX_PAGES = 400

DEFAULT_TOP_N = 30
MIN_TOP_N = 5
MAX_TOP_N = 50

# select_student_essential_urls guarantees topic coverage (admissions,
# tuition, housing, etc.) by never returning fewer than this many pages --
# it's a floor baked into that selector, not something a smaller `limit`
# can override. Below it there aren't enough slots to keep every topic
# represented, so a request for a genuinely small set (e.g. "just the 10-15
# main pages") is served from the navigation-derived base_important
# selector instead, which has no such floor.
STUDENT_ESSENTIAL_FLOOR = 30

# If an "active" job hasn't updated its progress in this long, treat it as
# dead (worker restart, OOM, a Celery hard time-limit kill -- anything that
# terminates the task without letting it reach a terminal status) instead of
# blocking every future discovery attempt forever.
STALE_ACTIVE_JOB_MINUTES = 10


def _canonical_base_url(raw_url: str) -> str:
    normalized = normalize_url(raw_url)
    if not normalized:
        raise ValueError("The university's website URL is missing or invalid.")
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def _reap_if_stale(job: DiscoveryJob) -> bool:
    """Mark a stuck active job as failed if it's gone quiet too long. Returns
    True if it was reaped (caller is then free to start a new job)."""
    cutoff = timezone.now() - timedelta(minutes=STALE_ACTIVE_JOB_MINUTES)
    if job.updated_at >= cutoff:
        return False
    job.status = "failed"
    job.error_message = (
        f"Automatically marked failed: no progress for over {STALE_ACTIVE_JOB_MINUTES} minutes "
        "(the worker process likely died or was restarted mid-crawl)."
    )
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
    return True


def start_discovery(
    university,
    max_pages: Optional[int] = None,
    top_n: Optional[int] = None,
    auto_apply: bool = True,
) -> DiscoveryJob:
    """Kick off a background crawl of `university.website_url` to propose
    candidate scrape_urls. Raises ValueError for anything the caller should
    surface as a 4xx (no website set, unreachable/private host, a job
    already running).

    When auto_apply is True (default) the Celery task automatically applies
    the top `top_n` recommended URLs to University.scrape_urls and runs
    scrape_now once the crawl completes -- no manual review step required.
    Pass auto_apply=False to only propose candidates via the results
    endpoint and apply them by hand.
    """
    if not (university.website_url or "").strip():
        raise ValueError(
            "Set the university's website URL first (see the profile's website_url field)."
        )

    active = university.discovery_jobs.filter(status__in=DiscoveryJob.ACTIVE_STATUSES).order_by("-created_at").first()
    if active and not _reap_if_stale(active):
        raise ValueError(f"A discovery job is already in progress for this university (job {active.id}).")

    base_url = _canonical_base_url(university.website_url)
    _host, root = validate_public_base_url(base_url)

    try:
        capped_pages = int(max_pages) if max_pages is not None else DEFAULT_MAX_PAGES
    except (TypeError, ValueError):
        capped_pages = DEFAULT_MAX_PAGES
    capped_pages = max(MIN_MAX_PAGES, min(capped_pages, MAX_MAX_PAGES))

    try:
        capped_top_n = int(top_n) if top_n is not None else DEFAULT_TOP_N
    except (TypeError, ValueError):
        capped_top_n = DEFAULT_TOP_N
    capped_top_n = max(MIN_TOP_N, min(capped_top_n, MAX_TOP_N))

    job = DiscoveryJob.objects.create(
        university=university,
        base_url=base_url,
        root_domain=root,
        settings={"max_pages": capped_pages},
        top_n=capped_top_n,
        auto_apply=bool(auto_apply),
    )

    from url_discovery.tasks import run_discovery_job

    run_discovery_job.delay(job.id)
    return job


def request_stop(job: DiscoveryJob) -> DiscoveryJob:
    if job.status not in DiscoveryJob.ACTIVE_STATUSES:
        raise ValueError(f"Cannot stop a job with status '{job.status}'.")
    job.status = "stop_requested"
    job.save(update_fields=["status", "updated_at"])
    return job


def recommended_urls(job: DiscoveryJob, mode: str = "student_essential", limit: Optional[int] = None) -> List[DiscoveredUrl]:
    """Rank this job's crawled pages into a curated candidate list.

    'student_essential' (default) targets a topic-diverse set of student-
    information pages, capped by `limit` (falls back to job.top_n, then
    DEFAULT_TOP_N) since each applied URL costs one scrape + one LLM fact
    extraction call. 'base_important' returns the full navigation-derived
    set instead, uncapped unless `limit` is given.
    """
    from url_discovery.important_urls import select_base_important_urls, select_student_essential_urls

    records = list(job.urls.all())
    if mode == "base_important":
        return select_base_important_urls(job, records, limit=limit)
    effective_limit = limit if limit is not None else (job.top_n or DEFAULT_TOP_N)
    return select_student_essential_urls(job, records, limit=effective_limit)


def apply_selected_urls(university, job: DiscoveryJob, urls: List[str], replace: bool = False) -> List[str]:
    """Merge selected discovered URLs into University.scrape_urls -- the
    exact same field the manual PUT /scrape-urls/ endpoint edits, so
    hand-entered URLs are preserved unless the caller explicitly replaces
    the list."""
    cleaned = [str(u).strip() for u in urls if isinstance(u, str) and str(u).strip()]
    if not cleaned:
        raise ValueError("No URLs were selected to apply.")

    existing: List[str] = [] if replace else list(university.scrape_urls or [])
    merged = list(dict.fromkeys(existing + cleaned))

    university.scrape_urls = merged
    university.save(update_fields=["scrape_urls", "updated_at"])

    job.applied_at = timezone.now()
    job.save(update_fields=["applied_at", "updated_at"])

    return merged


def run_auto_apply_and_scrape(job: DiscoveryJob) -> None:
    """Called by the Celery task right after a crawl completes when
    job.auto_apply is set: picks the top `job.top_n` candidate URLs, applies
    them to University.scrape_urls, and immediately runs the existing
    knowledge.scraper fact-extraction pipeline against them -- so a
    successful discovery run ends with a populated knowledge base with no
    further clicks required. Failures here are recorded on the job instead
    of raised, since this runs after the job already reached 'completed'."""
    from universities.services import scrape_now

    university = job.university
    try:
        # See STUDENT_ESSENTIAL_FLOOR: a small requested top_n can only be
        # honored by the uncapped navigation selector, not the topic-quota one.
        mode = "student_essential" if (job.top_n or DEFAULT_TOP_N) >= STUDENT_ESSENTIAL_FLOOR else "base_important"
        records = recommended_urls(job, mode=mode, limit=job.top_n)
        urls = [record.final_url or record.normalized_url for record in records]
        urls = [u for u in urls if u]

        if not urls:
            job.scrape_result = {
                "mode": mode,
                "applied_count": 0,
                "note": "No candidate URLs met the relevance bar for auto-apply.",
            }
            job.save(update_fields=["scrape_result", "updated_at"])
            return

        apply_selected_urls(university, job, urls, replace=False)
        scrape_result = scrape_now(university)
        job.scrape_result = {"mode": mode, "applied_urls": urls, **scrape_result}
        job.save(update_fields=["scrape_result", "updated_at"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-apply/scrape failed for discovery job %s", job.id)
        job.scrape_result = {"error": str(exc)[:2000]}
        job.save(update_fields=["scrape_result", "updated_at"])


def serialize_job(job: DiscoveryJob) -> Dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "base_url": job.base_url,
        "top_n": job.top_n,
        "auto_apply": job.auto_apply,
        "pages_discovered": job.pages_discovered,
        "pages_crawled": job.pages_crawled,
        "relevant_count": job.relevant_count,
        "review_count": job.review_count,
        "excluded_count": job.excluded_count,
        "failed_count": job.failed_count,
        "current_url": job.current_url,
        "error_message": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "applied_at": job.applied_at,
        "scrape_result": job.scrape_result,
        "created_at": job.created_at,
    }


def serialize_discovered_url(record: DiscoveredUrl, already_saved: set) -> Dict[str, Any]:
    url = record.final_url or record.normalized_url
    return {
        "id": record.id,
        "url": url,
        "page_title": record.page_title,
        "primary_category": record.primary_category,
        "relevance_score": record.relevance_score,
        "decision_status": record.decision_status,
        "already_saved": url in already_saved,
    }


# ---------------------------------------------------------------------------
# Cluster approval loop (B2) -- the human review step between "crawler found
# these" and "these facts are in the knowledge base under a department".
# ---------------------------------------------------------------------------

CLUSTER_STATUSES = ["relevant", "review"]


def serialize_cluster_approval(approval: DiscoveryClusterApproval) -> Dict[str, Any]:
    return {
        "category": approval.category,
        "knowledge_group": approval.knowledge_group.slug if approval.knowledge_group_id else None,
        "url_count": approval.url_count,
        "urls": approval.urls,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
    }


def proposed_department_map(job: DiscoveryJob) -> List[Dict[str, Any]]:
    """The 'proposed URL/department map' an officer reviews: this job's
    candidate pages grouped by classifier category, each tagged with the
    KnowledgeGroup it would feed once approved, and the approval record if
    that cluster has already been approved."""
    from url_discovery.classifier import category_config
    from url_discovery.group_mapping import group_slug_for_category

    university = job.university
    already_saved = set(university.scrape_urls or [])
    labels = category_config()

    approvals = {
        approval.category: approval
        for approval in DiscoveryClusterApproval.objects.filter(job=job)
    }

    records = list(job.urls.filter(decision_status__in=CLUSTER_STATUSES).order_by("-relevance_score"))
    by_category: Dict[str, List[DiscoveredUrl]] = {}
    for record in records:
        category = record.primary_category or "uncategorized"
        by_category.setdefault(category, []).append(record)

    clusters = []
    for category, category_records in sorted(by_category.items()):
        approval = approvals.get(category)
        clusters.append({
            "category": category,
            "label": labels.get(category, {}).get("label", category.replace("_", " ").title()),
            "knowledge_group_slug": group_slug_for_category(category) if category != "uncategorized" else None,
            "url_count": len(category_records),
            "urls": [serialize_discovered_url(r, already_saved) for r in category_records],
            "approved": serialize_cluster_approval(approval) if approval else None,
        })

    return clusters


def approve_cluster(job: DiscoveryJob, category: str, approved_by: str) -> Dict[str, Any]:
    """Approve one category cluster: apply its URLs to
    University.scrape_urls, scrape just those URLs, and record who/when
    (plus the category's mapped KnowledgeGroup, for the officer-facing
    department map) as provenance. Re-approving the same cluster updates
    the existing row (fresh provenance, not a duplicate).

    The scraped facts themselves are NOT tagged with the mapped group: a
    knowledge group collects escalations, not auto-scraped knowledge. Only
    a manual fact add routes into a group. The category->group mapping is
    kept purely as review metadata on the approval / department map."""
    from universities.knowledge_groups import ensure_default_groups
    from universities.models import KnowledgeGroup
    from universities.services import scrape_selected_urls
    from url_discovery.group_mapping import group_slug_for_category

    records = list(
        job.urls.filter(decision_status__in=CLUSTER_STATUSES, primary_category=category)
    )
    if not records:
        raise ValueError(f"No candidate URLs found for category '{category}' on this job.")

    university = job.university
    urls = [record.final_url or record.normalized_url for record in records]
    urls = list(dict.fromkeys(u for u in urls if u))

    ensure_default_groups(university)
    group_slug = group_slug_for_category(category)
    knowledge_group = KnowledgeGroup.objects.filter(university=university, slug=group_slug).first()

    apply_selected_urls(university, job, urls, replace=False)

    approved_at = timezone.now()
    approval, _created = DiscoveryClusterApproval.objects.update_or_create(
        job=job,
        category=category,
        defaults={
            "knowledge_group": knowledge_group,
            "url_count": len(urls),
            "urls": urls,
            "approved_by": approved_by,
            "approved_at": approved_at,
        },
    )

    # Intentionally no group_id: auto-scraped facts are never routed into a
    # knowledge group (that's escalation-only) -- see docstring.
    scrape_result = scrape_selected_urls(university, urls)

    return {
        "approval": serialize_cluster_approval(approval),
        "scrape_result": scrape_result,
    }
