"""Run only on the dedicated prefork chat queue; hard timeout kills stuck tools."""
import logging
from datetime import timedelta
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone
from django_api.models import ChatGeneration, ChatMessage
from django_api.chat_policy import (TurnBudget, TurnStopped, current_budget,
                                   TURN_SECONDS, HARD_SECONDS, QUEUE_SECONDS)

logger = logging.getLogger(__name__)


@shared_task(name="django_api.chat_tasks.generate_chat", soft_time_limit=TURN_SECONDS,
             time_limit=HARD_SECONDS, max_retries=0, acks_late=False, ignore_result=True)
def generate_chat(job_id):
    # Conditional claim makes duplicate delivery harmless. Stale queued jobs
    # cannot start after their admission lease has expired.
    now = timezone.now()
    claimed = ChatGeneration.objects.filter(pk=job_id, status="queued",
        created_at__gt=now - timedelta(seconds=QUEUE_SECONDS), expires_at__gt=now).update(
            status="running", started_at=now, expires_at=now + timedelta(seconds=HARD_SECONDS + 30))
    if not claimed:
        return
    job = ChatGeneration.objects.get(pk=job_id)
    budget = TurnBudget(job)
    token = current_budget.set(budget)
    state, code, result = "failed", "CHAT_FAILED", {"message": "Your agent could not finish. Please try again."}
    try:
        from pure_multi_agent.runtime import run_turn, seed_conversation
        from django_api.services import build_image_content_blocks
        from django_api.views import (_existing_pending_query_ids, _new_pending_query,
                                      _escalation_meta, _notify_agent_reply)
        target = ChatMessage.objects.get(pk=job.message_id, student_id=job.student_id)
        # A failed graph may have an unanswered tool-call checkpoint. Rebuild
        # from the visible transcript before recovery, excluding the new input.
        if not job.edit and ChatGeneration.objects.filter(student_id=job.student_id,
                status="failed", created_at__lt=job.created_at).exists():
            prior = list(ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT,
                student_id=job.student_id, pk__lt=target.pk).order_by("created_at").values_list("sender", "content"))
            seed_conversation(job.student_id, prior)
        if job.edit:
            prior = list(ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT,
                student_id=job.student_id, pk__lt=target.pk).order_by("created_at").values_list("sender", "content"))
            seed_conversation(job.student_id, prior)
            with transaction.atomic():
                ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT,
                    student_id=job.student_id, pk__gt=target.pk).delete()
                target.content = job.result["edited_message"]
                target.edited_at = timezone.now()
                target.save(update_fields=["content", "edited_at"])
        attachments = list(target.attachments.all())
        message = target.content.strip() or "Please review the attached files."
        existing = _existing_pending_query_ids(job.student_id)
        budget.check()
        name, reply = run_turn(job.student_id, message, image_blocks=build_image_content_blocks(attachments) or None)
        budget.check()
        pending = _new_pending_query(job.student_id, existing)
        ChatMessage.objects.create(channel=ChatMessage.Channel.AGENT, student_id=job.student_id,
            sender=ChatMessage.Sender.ASSISTANT, content=reply or "", meta=_escalation_meta(pending))
        result = {"agent": name, "student_id": job.student_id, "reply": reply,
            "message_id": target.pk, "edited_at": target.edited_at.isoformat() if target.edited_at else None,
            "pending": bool(pending), "query_id": pending.pk if pending else None}
        state, code = "completed", ""
    except (TurnStopped, SoftTimeLimitExceeded) as exc:
        code = getattr(exc, "code", "CHAT_TIMEOUT")
        result = {"code": code, "message": getattr(exc, "message", "Your agent took too long. Please try a shorter question.")}
    except Exception:
        # Never log prompts, provider payloads, session data or exception text.
        logger.warning("chat_generation_failed job_id=%s code=CHAT_FAILED", job_id)
    finally:
        current_budget.reset(token)
        finished = timezone.now()
        latency = int((finished - job.created_at).total_seconds() * 1000)
        ChatGeneration.objects.filter(pk=job_id, status="running").update(status=state,
            error_code=code, result=result, finished_at=finished, latency_ms=latency)
        logger.info("chat_generation job_id=%s status=%s code=%s latency_ms=%d", job_id, state, code, latency)

    if state == "completed":
        _notify_agent_reply(job.student_id, name, reply or "")
