# pure_multi_agent/tasks.py
# Background side-effects for agent-turn failures: alerting ops (off the
# request path, so a slow/unreachable SMTP server never adds to a student's
# already-failed turn) and probing for recovery.
from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True, soft_time_limit=570, time_limit=600, max_retries=3)
def execute_agent_job(self, job_id):
    from django.db import transaction
    from django.utils import timezone
    from django_api.models import AgentJob, ChatMessage
    from pure_multi_agent.capacity import lease, AgentBusy, ResumeTurnLater
    from pure_multi_agent.jobs import run
    job = AgentJob.objects.filter(pk=job_id).first()
    if not job or job.status != "queued":
        return
    try:
        with lease("thread:" + job.owner_key, ttl=900):
            if not AgentJob.objects.filter(pk=job_id, status="queued").update(status="processing", started_at=timezone.now()):
                return
            try:
                result, scope, metadata = run(job)
                with transaction.atomic():
                    current = AgentJob.objects.select_for_update().get(pk=job_id)
                    if current.status != "processing":
                        return
                    ChatMessage.objects.create(**scope, sender="assistant", content=result.get("reply", ""), meta=metadata)
                    current.status = "completed"
                    current.result = result
                    current.completed_at = timezone.now()
                    current.save(update_fields=["status", "result", "completed_at"])
                    if job.student_id and result.get("agent"):
                        from django_api.views import _notify_agent_reply
                        transaction.on_commit(lambda: _notify_agent_reply(job.student_id, result["agent"], result.get("reply", "")))
                logger.info("agent_job_completed job=%s kind=%s queue_seconds=%.3f execution_seconds=%.3f", job_id, job.kind,
                            (current.started_at - current.created_at).total_seconds(),
                            (current.completed_at - current.started_at).total_seconds())
            except ResumeTurnLater as exc:
                payload = {**job.payload, 'resume_state': exc.state}
                AgentJob.objects.filter(pk=job_id, status='processing').update(status='queued', payload=payload, dispatched_at=timezone.now())
                execute_agent_job.apply_async(args=[str(job_id)], countdown=exc.delay, queue='agent_chat', retry=False)
            except Exception:
                logger.exception("Agent job %s failed", job_id)
                AgentJob.objects.filter(pk=job_id, status="processing").update(status="failed", completed_at=timezone.now(),
                    error="The response could not be completed. Please retry your question.")
    except AgentBusy as exc:
        # No execution started; redelivery is safe here. Mid-turn failures are
        # never auto-replayed because tools may already have written profile data.
        raise self.retry(exc=exc, countdown=5)


@shared_task
def dispatch_agent_work():
    from datetime import timedelta
    from django.conf import settings
    from django.db.models import Q, F
    from django.utils import timezone
    from django_api.models import AgentJob, KnowledgeIndexWork
    from pure_multi_agent.jobs import dispatch
    now = timezone.now()
    AgentJob.objects.filter(status="queued", created_at__lt=now - timedelta(seconds=settings.AGENT_QUEUE_TIMEOUT)).update(
        status="failed", completed_at=now, error="Chat queue wait expired. Please retry.")
    AgentJob.objects.filter(status="processing", started_at__lt=now - timedelta(seconds=settings.AGENT_JOB_TIMEOUT + 60)).update(
        status="failed", completed_at=now, error="Chat execution expired. Please check your conversation before retrying.")
    pending = AgentJob.objects.filter(status="queued").filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lt=now - timedelta(seconds=120)))
    for pk in pending.order_by("created_at").values_list("pk", flat=True)[:100]:
        dispatch(pk)
    for uid in KnowledgeIndexWork.objects.filter(revision__gt=F("indexed_revision")).values_list("university_id", flat=True)[:100]:
        index_university.apply_async(args=[uid], queue="knowledge_index", retry=False)


@shared_task(soft_time_limit=570, time_limit=600)
def index_university(university_id):
    from django_api.models import KnowledgeIndexWork
    from knowledge.vectors import sync_embeddings, enabled
    from pure_multi_agent.capacity import lease, AgentBusy
    if not enabled():
        return
    try:
        with lease("index:" + university_id, ttl=900):
            work = KnowledgeIndexWork.objects.get(university_id=university_id)
            if work.indexed_revision >= work.revision:
                return
            try:
                sync_embeddings(university_id)
                KnowledgeIndexWork.objects.filter(pk=work.pk).update(indexed_revision=work.revision, error="")
            except Exception:
                logger.exception("Knowledge indexing failed for %s", university_id)
                KnowledgeIndexWork.objects.filter(pk=work.pk).update(error="Indexing failed; will retry.")
    except AgentBusy:
        return


@shared_task
def send_agent_error_alert_task(error_text: str, student_id: Optional[str] = None) -> None:
    from extra_utils.send_mail_to_superuser import notify_agent_error

    notify_agent_error(error_text, student_id=student_id)


@shared_task
def check_agent_recovery_task() -> None:
    """
    Scheduled every few minutes (see CELERY_BEAT_SCHEDULE), but only ever
    costs a single Redis read -- and, while an outage is flagged, one small
    model call -- never a full agent turn. This is how ops learns the agent
    is answering again without anyone watching logs or manually retrying:
    once the ping succeeds, it clears the outage flag and emails the same
    alert list that got the original failure notice.
    """
    from django.core.cache import cache

    from extra_utils.send_mail_to_superuser import AGENT_OUTAGE_CACHE_KEY, notify_agent_recovered

    if not cache.get(AGENT_OUTAGE_CACHE_KEY):
        return

    from langchain_core.messages import HumanMessage

    from pure_multi_agent.model_router import invoke

    try:
        invoke([HumanMessage(content="ping")])
    except Exception as exc:
        logger.info("check_agent_recovery_task: agent still unavailable (%s)", exc)
        return

    notify_agent_recovered()
