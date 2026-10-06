"""Durable chat jobs. HTTP admission never performs model calls."""
import logging
import time
import uuid
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F
from django.utils import timezone
from rest_framework.response import Response
from django_api.models import AgentJob, AgentQueueGate, ChatMessage
from pure_multi_agent.capacity import lease, AgentBusy, check_rate

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "processing")


def ready_jobs(limit, exclude=(), queue='chat'):
    """Round-robin tenants, with two active workflows each, before model scheduling."""
    from collections import Counter, defaultdict, deque
    from django.db.models import Q
    from pure_multi_agent.inference_admission import tenant_key
    counts = Counter(tenant_key(key) for key in AgentJob.objects.filter(
        Q(status='processing') | Q(pk__in=list(exclude))).values_list('owner_key', flat=True))
    groups = defaultdict(deque)
    rows = AgentJob.objects.filter(status='queued').filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lte=timezone.now())).exclude(pk__in=exclude)
    rows = rows.filter(kind__in=['resume','linkedin']) if queue == 'documents' else rows.exclude(kind__in=['resume','linkedin'])
    rows = rows.order_by('created_at')[:1000]
    for row in rows:
        groups[tenant_key(row.owner_key)].append(row.pk)
    selected = []
    while len(selected) < limit:
        progress = False
        for tenant, jobs in groups.items():
            if jobs and counts[tenant] < 2 and len(selected) < limit:
                selected.append(jobs.popleft()); counts[tenant] += 1; progress = True
        if not progress:
            break
    return selected


def owner_key(request, university_id=None, subject_student_id=None):
    if university_id:
        return 'university:' + str(university_id) + (':student:' + str(subject_student_id) if subject_student_id else '')
    return 'student:' + str(request.user.account.student_uuid)


def serialize(job):
    data = {"job_id": str(job.pk), "status": job.status, "poll_after_ms": 1500}
    data['recovery_attempts'] = job.recovery_attempts
    if job.kind in ('resume', 'linkedin'):
        from pure_multi_agent.document_progress import public_progress
        data['progress'] = public_progress(job)
    if job.status == "completed":
        data["result"] = job.result
    if job.status == "failed":
        data["error"] = job.error
    return data


def dispatch(job_id):
    if settings.AGENT_QUEUE_BACKEND == 'database':
        return  # The durable database worker polls committed jobs directly.
    from pure_multi_agent.tasks import execute_agent_job
    try:
        kind = AgentJob.objects.filter(pk=job_id).values_list('kind', flat=True).first()
        execute_agent_job.apply_async(args=[str(job_id)], queue='agent_documents' if kind in ('resume','linkedin') else 'agent_chat', retry=False)
        AgentJob.objects.filter(pk=job_id, status="queued").update(dispatched_at=timezone.now())
    except Exception:
        logger.exception("Agent job %s is persisted; dispatcher will retry publication", job_id)


def submit(request, university_id=None, message_id=None, subject_student_id=None):
    # Keep retries tied to one submission, including clients without a key.
    idem = request.headers.get("Idempotency-Key", "") or str(uuid.uuid4())
    for attempt in range(4):
        try:
            return _submit(request, university_id, message_id, subject_student_id,
                           idempotency_key=idem, check_limit=attempt == 0)
        except OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            # Uploaded files have filesystem side effects outside the DB
            # transaction; don't replay them automatically after a rollback.
            if attempt == 3 or request.FILES:
                logger.warning("Chat admission database busy; returning a retryable JSON response")
                return Response({"message": "Chat is temporarily busy. Please try sending again in a moment."},
                                status=503, headers={"Retry-After": "2"})
            time.sleep(0.05 * (attempt + 1))


def _submit(request, university_id=None, message_id=None, subject_student_id=None,
            *, idempotency_key, check_limit=True):
    key = owner_key(request, university_id, subject_student_id)
    idem = idempotency_key
    if len(idem) > 100:
        return Response({"message": "Idempotency-Key is too long."}, status=400)
    previous = AgentJob.objects.filter(owner_key=key, idempotency_key=idem).first()
    if previous:
        return Response(serialize(previous), status=200 if previous.status == "completed" else 202)
    try:
        if check_limit:
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
        with lease("thread:" + key, ttl=900), lease('queue:admission', ttl=10, wait=2), transaction.atomic():
            # Acquire the writer lock before reading on SQLite. A deferred
            # read-then-write transaction can fail immediately when another
            # worker writes, regardless of the configured busy timeout.
            if not AgentQueueGate.objects.filter(pk=1).update(id=F("id")):
                AgentQueueGate.objects.get_or_create(pk=1)
            AgentQueueGate.objects.select_for_update().get(pk=1)
            previous = AgentJob.objects.filter(owner_key=key, idempotency_key=idem).first()
            if previous:
                return Response(serialize(previous), status=200 if previous.status == "completed" else 202)
            if AgentJob.objects.filter(owner_key=key, status__in=ACTIVE).exists():
                return Response({"message": "A response is already in progress. Wait for it before sending or editing."}, status=409)
            if AgentJob.objects.filter(status__in=ACTIVE).count() >= settings.AGENT_QUEUE_CAPACITY:
                return Response({"message": "Chat is at capacity. Please retry shortly."}, status=429, headers={"Retry-After": "5"})
            student_id = str(subject_student_id or '') if university_id else str(request.user.account.student_uuid)
            if message_id:
                target = ChatMessage.objects.filter(pk=message_id, student_id=student_id, channel="agent", sender="user").first()
                if target is None:
                    return Response({"message": "Message not found."}, status=404)
            else:
                target = ChatMessage.objects.create(channel="presenter" if subject_student_id else "university" if university_id else "agent", student_id=student_id,
                    university_id=str(university_id or ""), sender="user", content=message.strip())
                for uploaded in files:
                    save_chat_attachment(student_id, target, uploaded)
            job = AgentJob.objects.create(owner_key=key, idempotency_key=idem,
                kind="university" if university_id else "student_edit" if message_id else "student",
                student_id=student_id, university_id=str(university_id or ""),
                payload={"message_id": target.pk, "message": message.strip(), "actor_id": request.user.pk,
                    **({'subject_student_id': str(subject_student_id)} if subject_student_id else {})})
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
        key = owner_key(request, kwargs.get("university_id"), kwargs.get('student_id'))
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
    if job.kind in ('resume', 'linkedin'):
        from pure_multi_agent.document_jobs import run_document
        return run_document(job)
    from django_api.views import build_image_content_blocks, record_chat_university_interests, _existing_pending_query_ids, _new_pending_query, _escalation_meta
    target = ChatMessage.objects.get(pk=job.payload["message_id"])
    message = job.payload["message"] or "Please review the attached files."
    if job.kind == "university":
        from pure_multi_agent.officer_graph import run_turn as officer_turn
        subject = job.payload.get('subject_student_id')
        scope = {'channel': 'presenter' if subject else 'university', 'university_id': job.university_id, 'student_id': subject or ''}
        previous = list(ChatMessage.objects.filter(**scope, pk__lt=target.pk)
                        .order_by("-created_at", "-pk")[:10])[::-1]
        history = [{"role": "user" if row.sender == "user" else "assistant", "content": row.content} for row in previous]
        result = officer_turn(job.university_id, job.payload.get('actor_id'), message,
            turn_id=str(target.pk), history=history, resume_state=job.payload.get('resume_state'),
            **({'subject_student_id': subject} if subject else {}))
        result["reply"] = result.get("answer", "")
        return result, scope, result
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
    turn_options = {'raise_errors': True, 'message_id': target.pk}
    if 'resume_state' in job.payload:
        turn_options['resume_state'] = job.payload['resume_state']
    turn_result = run_turn(job.student_id, message, image_blocks=build_image_content_blocks(target.attachments.all()) or None, **turn_options)
    cache = getattr(turn_result, 'university_cache', {})
    if cache.get('pages') or cache.get('missing') or cache.get('research'):
        from django.core.serializers.json import DjangoJSONEncoder
        import json
        from django.db import transaction
        with transaction.atomic():
            current = AgentJob.objects.select_for_update().get(pk=job.pk)
            current.payload = {**current.payload, 'university_cache_pending':json.loads(json.dumps(cache, cls=DjangoJSONEncoder))}
            current.save(update_fields=['payload'])
    name, reply = turn_result
    turn_meta = getattr(turn_result, "metadata", {})
    pending = _new_pending_query(job.student_id, prior_queries)
    result = {"agent": name, "student_id": job.student_id, "reply": reply, "meta": turn_meta, "message_id": target.pk,
              "pending": bool(pending), "query_id": pending.pk if pending else None}
    return result, {"channel": "agent", "student_id": job.student_id}, {**_escalation_meta(pending), **turn_meta}
