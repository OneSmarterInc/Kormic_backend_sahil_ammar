"""Short authenticated HTTP requests for durable, owner-scoped chat jobs."""
from datetime import timedelta
from pathlib import Path
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django_api.models import ChatGeneration, ChatMessage, StudentProfile, ChatModelCall
from django_api.chat_policy import (ChatPolicyError, gate, limit, LEASE_SECONDS,
    MAX_MESSAGE_CHARS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, MAX_TOTAL_ATTACHMENT_BYTES)
from django_api.views import STUDENT_PERMISSIONS, _serialize_attachment

ACTIVE = ("queued", "running")


def policy_response(exc):
    return Response({"status": "failed", "code": exc.code, "message": exc.message},
                    status=exc.http_status, headers={"Retry-After": "60"} if exc.http_status == 429 else {})


def expire_jobs(student_id=None):
    qs = ChatGeneration.objects.filter(status__in=ACTIVE, expires_at__lte=timezone.now())
    if student_id:
        qs = qs.filter(student_id=student_id)
    for job in qs.iterator():
        ChatGeneration.objects.filter(pk=job.pk, status__in=ACTIVE).update(
            status="failed", error_code="CHAT_TIMEOUT", finished_at=job.expires_at,
            latency_ms=max(0, int((job.expires_at - job.created_at).total_seconds() * 1000)),
            result={"code": "CHAT_TIMEOUT", "message": "Your agent took too long. Please try again."})


def assert_idle(student_id):
    expire_jobs(student_id)
    if ChatGeneration.objects.filter(student_id=student_id, status__in=ACTIVE).exists():
        raise ChatPolicyError("CHAT_IN_PROGRESS", "Your agent is still working on your previous request.")


def admit(student_id):
    # Caller holds the global admission mutex, serializing admission across
    # workers; the student row also coordinates model-budget reservations.
    StudentProfile.objects.select_for_update().get(uuid=student_id)
    assert_idle(student_id)
    if ChatGeneration.objects.filter(student_id=student_id,
        created_at__gte=timezone.now() - timedelta(seconds=60)).count() >= limit("CHAT_REQUESTS_PER_MINUTE", 6):
        raise ChatPolicyError("CHAT_RATE_LIMIT", "Too many chat requests. Please wait a minute.")
    if ChatGeneration.objects.filter(status__in=ACTIVE, expires_at__gt=timezone.now()).count() >= limit("CHAT_GLOBAL_PENDING_LIMIT", 100):
        raise ChatPolicyError("CHAT_BUSY", "Chat is busy. Please try again shortly.")
    from django.db.models import Sum
    from decimal import Decimal
    totals = ChatModelCall.objects.filter(generation__student_id=student_id,
        created_at__date=timezone.now().date()).aggregate(tokens=Sum("charged_tokens"), cost=Sum("estimated_cost_usd"))
    if ((totals["tokens"] or 0) >= limit("CHAT_DAILY_TOKENS", 250000) or
        (totals["cost"] or 0) >= Decimal(__import__("os").environ.get("CHAT_DAILY_USD", "5"))):
        raise ChatPolicyError("CHAT_DAILY_BUDGET", "Your daily AI allowance has been reached. Please try again tomorrow.")


def submit(request, message_id=None):
    from django_api.services import save_chat_attachment, CHAT_ATTACHMENT_ALLOWED_TYPES
    from django_api.chat_tasks import generate_chat
    student_id = request.user.account.student_uuid
    if not student_id:
        return policy_response(ChatPolicyError("CHAT_PROFILE_REQUIRED", "Create your student profile first.", 400))
    from django_api.chat_uploads import ChatUploadBudgetHandler
    upload_budget = ChatUploadBudgetHandler(request._request)
    request._request.upload_handlers.insert(0, upload_budget)
    message = str(request.data.get("message") or "").strip()
    files = request.FILES.getlist("attachments")
    if upload_budget.exceeded:
        return policy_response(ChatPolicyError("CHAT_ATTACHMENT_LIMIT", "Attach at most 3 files, 5 MB each and 10 MB total.", 413))
    if (not message and not files) or (message_id is not None and not message):
        return policy_response(ChatPolicyError("CHAT_EMPTY_MESSAGE", "A message or attachment is required.", 400))
    if len(message) > MAX_MESSAGE_CHARS:
        return policy_response(ChatPolicyError("CHAT_MESSAGE_TOO_LARGE", f"Use at most {MAX_MESSAGE_CHARS} characters.", 413))
    if (len(files) > MAX_ATTACHMENTS or sum(f.size for f in files) > MAX_TOTAL_ATTACHMENT_BYTES or
        any(f.size > MAX_ATTACHMENT_BYTES for f in files)):
        return policy_response(ChatPolicyError("CHAT_ATTACHMENT_LIMIT", "Attach at most 3 files, 5 MB each and 10 MB total.", 413))
    if any(f.content_type not in CHAT_ATTACHMENT_ALLOWED_TYPES for f in files):
        return policy_response(ChatPolicyError("CHAT_ATTACHMENT_TYPE", "Unsupported attachment type.", 400))
    attachments = []
    try:
        with gate("chat-admission"):
            admit(student_id)
            if message_id is not None:
                target = ChatMessage.objects.filter(pk=message_id, student_id=student_id,
                    channel=ChatMessage.Channel.AGENT, sender=ChatMessage.Sender.USER).first()
                if target is None:
                    raise ChatPolicyError("CHAT_MESSAGE_NOT_FOUND", "Message not found.", 404)
                attachments = list(target.attachments.all())
                if (len(attachments) > MAX_ATTACHMENTS or
                    sum(a.size_bytes for a in attachments) > MAX_TOTAL_ATTACHMENT_BYTES or
                    any(a.size_bytes > MAX_ATTACHMENT_BYTES for a in attachments)):
                    raise ChatPolicyError("CHAT_ATTACHMENT_LIMIT", "This older message exceeds the attachment budget. Send a new message.", 413)
                # Store the edit in the job until the worker owns the generation.
                job_payload = {"edited_message": message}
            else:
                target = ChatMessage.objects.create(channel=ChatMessage.Channel.AGENT,
                    student_id=student_id, sender=ChatMessage.Sender.USER, content=message)
                for f in files:
                    attachments.append(save_chat_attachment(student_id, target, f))
                job_payload = {}
            job = ChatGeneration.objects.create(student_id=student_id, message_id=target.pk,
                account_id=request.user.account.pk, request_id=getattr(request, "request_id", ""),
                edit=message_id is not None, result=job_payload,
                expires_at=timezone.now() + timedelta(seconds=LEASE_SECONDS))
    except ChatPolicyError as exc:
        return policy_response(exc)
    except Exception:
        if message_id is None:
            for a in attachments:
                Path(a.file_path).unlink(missing_ok=True)
        return policy_response(ChatPolicyError("CHAT_UNAVAILABLE", "Chat is temporarily unavailable.", 503))
    try:
        generate_chat.apply_async(args=[str(job.pk)], task_id=str(job.pk), queue="chat",
                                  expires=60, retry=False)
    except Exception:
        # A publish timeout is ambiguous. Keep the admission lease until its
        # expiry, so retrying cannot start a second generation for this user.
        return Response({"job_id": str(job.pk), "status": "queued", "code": "CHAT_QUEUE_UNCERTAIN",
            "message": "Checking whether your request was accepted.",
            "poll_after_ms": 2000}, status=202)
    return Response({"job_id": str(job.pk), "status": "queued", "message_id": target.pk,
        "attachments": [_serialize_attachment(request, a) for a in attachments],
        "poll_after_ms": 1500}, status=202)


@api_view(["POST"])
@permission_classes(STUDENT_PERMISSIONS)
def chat_submit(request):
    return submit(request)


@api_view(["PATCH"])
@permission_classes(STUDENT_PERMISSIONS)
def chat_edit(request, message_id):
    return submit(request, message_id)


@api_view(["GET"])
@permission_classes(STUDENT_PERMISSIONS)
def chat_status(request, job_id):
    student_id = request.user.account.student_uuid
    expire_jobs(student_id)
    job = ChatGeneration.objects.filter(pk=job_id, student_id=student_id).first()
    if job is None:
        return policy_response(ChatPolicyError("CHAT_JOB_NOT_FOUND", "Chat request not found.", 404))
    result = job.result if job.status in ("completed", "failed") else {}
    return Response({**result, "job_id": str(job.pk), "status": job.status,
                     "code": job.error_code or None, "poll_after_ms": 1500})


@api_view(["POST"])
@permission_classes(STUDENT_PERMISSIONS)
def chat_new(request):
    student_id = request.user.account.student_uuid
    try:
        with gate("chat-admission"):
            assert_idle(student_id)
            from pure_multi_agent.runtime import reset_conversation
            reset_conversation(student_id)
            deleted, _ = ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT, student_id=student_id).delete()
        return Response({"status": "ok", "student_id": student_id, "messages_deleted": deleted})
    except ChatPolicyError as exc:
        return policy_response(exc)
