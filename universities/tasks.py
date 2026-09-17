# universities/tasks.py
# Background execution for ScrapeNowAPIView's fact-extraction pipeline.
# Mirrors url_discovery/tasks.py's run_discovery_job: its own generous
# soft/hard time limits (the project's global CELERY_TASK_TIME_LIMIT=30 is
# sized for quick tasks like push notifications), and the job row is marked
# 'failed' on any exception instead of letting Celery's retry/dead-letter
# behavior leave it stuck in 'running' forever.
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# A scrape run does one knowledge.scraper LLM extraction call per URL, plus
# a be-polite time.sleep(1.5) per URL (see knowledge/scraper.py) -- a
# university with dozens of scrape URLs can legitimately take several
# minutes. Sized generously above that, same reasoning as
# url_discovery.tasks.DISCOVERY_SOFT_TIME_LIMIT/HARD_TIME_LIMIT.
SCRAPE_SOFT_TIME_LIMIT = 15 * 60
SCRAPE_HARD_TIME_LIMIT = 18 * 60


@shared_task(
    bind=True,
    soft_time_limit=SCRAPE_SOFT_TIME_LIMIT,
    time_limit=SCRAPE_HARD_TIME_LIMIT,
)
def run_scrape_now_job(self, job_id: int) -> None:
    from universities import services
    from universities.models import ScrapeJob

    job = ScrapeJob.objects.filter(id=job_id).select_related("university").first()
    if job is None:
        logger.warning("run_scrape_now_job: ScrapeJob %s no longer exists.", job_id)
        return

    job.status = ScrapeJob.Status.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    try:
        result = services.scrape_now(job.university)
    except Exception as exc:
        logger.exception("Scrape job %s failed", job_id)
        job.status = ScrapeJob.Status.FAILED
        job.error_message = str(exc)[:1000]
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        return

    job.status = ScrapeJob.Status.COMPLETED
    job.result = result
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "result", "completed_at"])
