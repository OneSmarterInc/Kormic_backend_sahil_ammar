from __future__ import annotations

import logging

import httpx
from celery import shared_task
from django.db import InterfaceError, OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)

# Narrowly scoped to genuinely transient infrastructure blips (a DB hiccup
# or network error before/while the crawler is getting started) rather than
# a blanket `except Exception` retry -- the crawler has real side effects
# (DB writes, an eventual scrape_now call on auto_apply), so blindly
# retrying an arbitrary failure that happened deep into a 25-minute crawl
# risks duplicate work. Anything else still fails the job immediately, same
# as before.
_RETRYABLE_EXCEPTIONS = (OperationalError, InterfaceError, httpx.TransportError, httpx.TimeoutException)

# The project's global CELERY_TASK_TIME_LIMIT is 30s, sized for quick tasks
# like push notifications. A discovery crawl legitimately runs for minutes,
# so it needs its own generous limit here -- otherwise Celery hard-kills
# (SIGKILL) the worker process partway through, which can't be caught in
# Python and leaves the DiscoveryJob permanently stuck in whatever status it
# had at that instant. soft_time_limit raises a catchable exception first so
# the job can still be marked 'failed' cleanly; time_limit is the SIGKILL
# backstop, set well above what even a full max_pages=400 crawl needs.
DISCOVERY_SOFT_TIME_LIMIT = 25 * 60
DISCOVERY_HARD_TIME_LIMIT = 30 * 60


@shared_task(
    bind=True,
    max_retries=1,
    default_retry_delay=15,
    soft_time_limit=DISCOVERY_SOFT_TIME_LIMIT,
    time_limit=DISCOVERY_HARD_TIME_LIMIT,
)
def run_discovery_job(self, job_id: int) -> None:
    """Runs a DiscoveryJob's crawl, then -- if the job is set to auto_apply
    and the crawl completed cleanly -- applies the top candidate URLs to
    University.scrape_urls and runs the existing scrape_now fact-extraction
    pipeline immediately, so a discovery run can end with a populated
    knowledge base with no further manual steps."""
    from url_discovery.crawler import DirectUniversityCrawler
    from url_discovery.models import DiscoveryJob

    try:
        DirectUniversityCrawler(job_id).run()
    except _RETRYABLE_EXCEPTIONS as exc:
        if self.request.retries < self.max_retries:
            logger.warning("Discovery job %s hit a transient error, retrying: %s", job_id, exc)
            raise self.retry(exc=exc)
        logger.exception("Discovery job %s failed to start (out of retries)", job_id)
        DiscoveryJob.objects.filter(id=job_id).update(
            status="failed",
            error_message="Unexpected error starting the crawler.",
            completed_at=timezone.now(),
        )
        return
    except Exception:
        logger.exception("Discovery job %s failed to start", job_id)
        DiscoveryJob.objects.filter(id=job_id).update(
            status="failed",
            error_message="Unexpected error starting the crawler.",
            completed_at=timezone.now(),
        )
        return

    job = DiscoveryJob.objects.filter(id=job_id).select_related("university").first()
    if job and job.status == "completed" and job.auto_apply:
        from url_discovery.services import run_auto_apply_and_scrape

        run_auto_apply_and_scrape(job)
