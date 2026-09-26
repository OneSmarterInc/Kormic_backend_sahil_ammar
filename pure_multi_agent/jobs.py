"""Durable chat jobs. HTTP admission never performs model calls."""
import logging
import uuid
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import transaction, IntegrityError
from django.utils import timezone
from rest_framework.response import Response
from django_api.models import AgentJob, AgentQueueGate, ChatMessage
from pure_multi_agent.capacity import lease, AgentBusy, check_rate

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "processing")


def owner_key(request, university_id=None):
    return "university:" + str(university_id) if university_id else "student:" + str(request.user.account.student_uuid)


def serialize(job):
    data = {"job_id": str(job.pk), "status": job.status, "poll_after_ms": 1500}
    if job.status == "completed":
        data["result"] = job.result
    if job.status == "failed":
        data["error"] = job.error
    return data


def dispatch(job_id):
    from pure_multi_agent.tasks import execute_agent_job
    try:
        execute_agent_job.apply_async(args=[str(job_id)], queue="agent_chat", retry=False)
        AgentJob.objects.filter(pk=job_id, status="queued").update(dispatched_at=timezone.now())
    except Exception:
        logger.exception("Agent job %s is persisted; dispatcher will retry publication", job_id)


def submit(request, university_id=None, message_id=None):
    key = owner_key(request, university_id)
    idem = request.headers.get("Idempotency-Key", "") or str(uuid.uuid4())
    if len(idem) > 100:
        return Response({"message": "Idempotency-Key is too long."}, status=400)
    previous = AgentJob.objects.filter(owner_key=key, idempotency_key=idem).first()
    if previous:
        return Response(serialize(previous), status=200 if previous.status == "completed" else 202)
    try:
        check_rate("submit:" + key, settings.AGENT_STUDENT_REQUESTS_PER_MINUTE)
    except AgentBusy as exc:
        return Response({"message": str(exc)}, status=429, headers={"Retry-After": "60"})
    message = request.data.get("message", request.data.get("question", "") if university_id else "")
    files = request.FILES.getlist("attachments")
    if not isinstance(message, str) or len(message) > 12000 or (not message.strip() and not files):
        return Response({"message": "Provide a message of up to 12000 characters or an attachment."}, status=400)
    from django_api.views import save_chat_attachment, CHAT_ATTACHMENT_MAX_PER_MESSAGE
    if len(files) > CHAT_ATTACHMENT_MAX_PER_MESSAGE or (university_id and files):
        return Response({"message": "Invalid attachments."}, status=400)
    try:
        # This lock also excludes clear/edit while admission writes the transcript.
        with lease("thread:" + key, ttl=900), transaction.atomic():
            AgentQueueGate.objects.get_or_create(pk=1)
            AgentQueueGate.objects.select_for_update().get(pk=1)
            previous = AgentJob.objects.filter(owner_key=key, idempotency_key=idem).first()
            if previous:
                return Response(serialize(previous), status=200 if previous.status == "completed" else 202)
            if AgentJob.objects.filter(owner_key=key, status__in=ACTIVE).exists():
                return Response({"message": "A response is already in progress. Wait for it before sending or editing."}, status=409)
            if AgentJob.objects.filter(status__in=ACTIVE).count() >= settings.AGENT_QUEUE_CAPACITY:
                return Response({"message": "Chat is at capacity. Please retry shortly."}, status=429, headers={"Retry-After": "5"})
            student_id = "" if university_id else str(request.user.account.student_uuid)
            if message_id:
                target = ChatMessage.objects.filter(pk=message_id, student_id=student_id, channel="agent", sender="user").first()
                if target is None:
                    return Response({"message": "Message not found."}, status=404)
            else:
                target = ChatMessage.objects.create(channel="university" if university_id else "agent", student_id=student_id,
                    university_id=str(university_id or ""), sender="user", content=message.strip())
                for uploaded in files:
                    save_chat_attachment(student_id, target, uploaded)
            job = AgentJob.objects.create(owner_key=key, idempotency_key=idem,
                kind="university" if university_id else "student_edit" if message_id else "student",
                student_id=student_id, university_id=str(university_id or ""),
                payload={"message_id": target.pk, "message": message.strip()})
            transaction.on_commit(lambda: dispatch(job.pk))
        return Response(serialize(job), status=202)
    except (AgentBusy, IntegrityError):
        return Response({"message": "A response is already in progress. Please retry shortly."}, status=409)
    except ValueError as exc:
        return Response({"message": str(exc)}, status=400)


def guarded_history(fn):
    """Clear/edit share a lease with job execution and check durable queued work."""
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        if request.method == "GET":
            return fn(request, *args, **kwargs)
        key = owner_key(request, kwargs.get("university_id"))
        try:
            with lease("thread:" + key, ttl=900):
                if AgentJob.objects.filter(owner_key=key, status__in=ACTIVE).exists():
                    return Response({"message": "Wait for the current response before changing this conversation."}, status=409)
                return fn(request, *args, **kwargs)
        except AgentBusy:
            return Response({"message": "This conversation is busy."}, status=409)
    return wrapped


def run(job):
    """Execute once; recovery never blindly replays a partially executed turn."""
    from django_api.views import build_image_content_blocks, record_chat_university_interests, _existing_pending_query_ids, _new_pending_query, _escalation_meta
    target = ChatMessage.objects.get(pk=job.payload["message_id"])
    message = job.payload["message"] or "Please review the attached files."
    if job.kind == "university":
        from agents.commons import get_university_agent
        previous = list(ChatMessage.objects.filter(channel="university", university_id=job.university_id, student_id="", pk__lt=target.pk)
                        .order_by("-created_at", "-pk")[:10])[::-1]
        history = [{"role": "user" if row.sender == "user" else "assistant", "content": row.content} for row in previous]
        result = get_university_agent(job.university_id).answer(message, caller_role="officer", history=history)
        result["reply"] = result.get("answer", "")
        return result, {"channel": "university", "university_id": job.university_id, "student_id": ""}, result
    from pure_multi_agent.runtime import run_turn, seed_conversation
    if job.kind == "student_edit" and "resume_state" not in job.payload:
        previous = list(ChatMessage.objects.filter(channel="agent", student_id=job.student_id, pk__lt=target.pk).order_by("created_at", "pk").values_list("sender", "content"))
        ChatMessage.objects.filter(channel="agent", student_id=job.student_id, pk__gt=target.pk).delete()
        target.content = message
        target.edited_at = timezone.now()
        target.save(update_fields=["content", "edited_at"])
        seed_conversation(job.student_id, previous)
    record_chat_university_interests(job.student_id, message)
    prior_queries = _existing_pending_query_ids(job.student_id)
    turn_options = {'raise_errors': True}
    if 'resume_state' in job.payload:
        turn_options['resume_state'] = job.payload['resume_state']
    turn_result = run_turn(job.student_id, message, image_blocks=build_image_content_blocks(target.attachments.all()) or None, **turn_options)
    name, reply = turn_result
    turn_meta = getattr(turn_result, "metadata", {})
    pending = _new_pending_query(job.student_id, prior_queries)
    result = {"agent": name, "student_id": job.student_id, "reply": reply, "meta": turn_meta, "message_id": target.pk,
              "pending": bool(pending), "query_id": pending.pk if pending else None}
    return result, {"channel": "agent", "student_id": job.student_id}, {**_escalation_meta(pending), **turn_meta}
