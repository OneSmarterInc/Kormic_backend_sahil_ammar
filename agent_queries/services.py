"""Durable, tenant-scoped agent questions. Human answers are evidence, not prompts."""
import hashlib
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from accounts.models import Account
from django_api.models import StudentProfile, UniversityInterestEvent, UniversityKnowledgeEntry
from universities.models import University
from notifications.services import notify_account, notify_university
from .models import AgentConversation, AgentConversationMessage, AgentQuery


def conversation_for(student_id, university_id, *, require_interest=False):
    student = StudentProfile.objects.get(uuid=student_id)
    university = University.objects.get(uuid=university_id)
    if require_interest and not UniversityInterestEvent.objects.filter(student=student, university_id=str(university.uuid)).exists():
        raise PermissionDenied("This student has not expressed interest in your university.")
    row, _ = AgentConversation.objects.get_or_create(student=student, university=university)
    return row


def names(conversation):
    return {"student_agent": conversation.student.agent_name or (conversation.student.name or "Student") + "'s agent",
            "university_agent": conversation.university.agent_name or conversation.university.name + " agent",
            "student": conversation.student.name or "Student", "university": conversation.university.name}


def message(conversation, actor, content, *, kind="message", query=None, metadata=None):
    from pure_multi_agent.telemetry import current, safe_data
    trace = current()
    metadata = safe_data({**{key: trace[key] for key in ('run_id', 'exchange_id') if trace.get(key)}, **(metadata or {})})
    row = AgentConversationMessage.objects.create(conversation=conversation, actor=actor,
        actor_name=names(conversation).get(actor, actor), content=str(content)[:45000], kind=kind, query=query, metadata=metadata or {})
    AgentConversation.objects.filter(pk=conversation.pk).update(updated_at=timezone.now())
    from pure_multi_agent.telemetry import emit, current
    agents = names(conversation)
    recipient = 'university_agent' if actor in ('student_agent', 'student') else 'student_agent'
    emit('AGENT_CONVERSATION_MESSAGE', agents[recipient], actor=agents.get(actor, actor),
        student_id=str(conversation.student.uuid), run_id=current().get('run_id') or str(conversation.pk),
        outputs={'conversation_id': str(conversation.pk), 'message_id': row.pk, 'kind': kind,
                 'content': row.content, 'query_id': row.query_id})
    return row


def history(conversation):
    rows = list(conversation.messages.filter(kind__in=["request", "reply", "human_answer"]).order_by("-id")[:16])
    return [{"speaker": r.actor_name, "content": r.content, "timestamp": r.created_at.isoformat()} for r in reversed(rows)]


def answered_evidence(conversation, direction):
    return list(conversation.queries.filter(direction=direction, status="answered").order_by("-answered_at").values(
        "id", "question", "answer", "answered_at", "answer_scope")[:30])


def notify_query(query, *, answered=False):
    conv = query.conversation
    to_student = (query.direction == AgentQuery.Direction.UNIVERSITY) != answered
    payload = {"type": "agent_query_answered" if answered else "agent_query_raised", "query_id": query.pk,
        "question": query.question, "answer": query.answer if answered else "", "university_name": conv.university.name,
        "university_id": str(conv.university.uuid), "student_name": conv.student.name,
        "raised_by_agent": query.raised_by_agent, "status": query.status, "direction": query.direction,
        "route": "queries" if to_student else f"/university/{conv.university.uuid}/agent-queries?query={query.pk}"}
    title = f"{conv.university.name}: answer received" if answered and to_student else (
        f"{conv.university.name} has a question" if to_student else ("Student answer received" if answered else "Agent query needs your answer"))
    body = query.answer if answered else query.question
    if to_student:
        account = Account.objects.filter(role="student", student_profile=conv.student).first()
        if account:
            log = notify_account(account=account, event_type="pending_query_resolved" if answered else "university_query",
                title=title[:255], body=body, data=payload)
            def push_after_commit():
                try:
                    from notifications.tasks import send_push_notification_task
                    send_push_notification_task.delay(log.id)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("Push unavailable for query %s; durable inbox notification remains", query.pk)
            transaction.on_commit(push_after_commit)
    else:
        notify_university(str(conv.university.uuid), event_type="pending_query_resolved" if answered else "university_query",
            title=title, body=body, data=payload)


@transaction.atomic
def raise_query(conversation, direction, question):
    question = question.strip()
    if not question or len(question) > 2000:
        raise ValueError("A specific question of 1â€“2000 characters is required.")
    actor = "student_agent" if direction == AgentQuery.Direction.STUDENT else "university_agent"
    target = "university_agent" if actor == "student_agent" else "student_agent"
    # Serialize the pair so concurrent model retries cannot create duplicate notifications.
    AgentConversation.objects.select_for_update().get(pk=conversation.pk)
    fingerprint = hashlib.sha256(" ".join(question.casefold().split()).encode()).hexdigest()
    row, created = AgentQuery.objects.get_or_create(conversation=conversation, direction=direction,
        question_hash=fingerprint, status="unanswered", defaults={"question": question,
        "raised_by_agent": names(conversation)[actor], "recipient_agent": names(conversation)[target]})
    if created:
        message(conversation, target, "This information is missing. I have asked my user to answer: " + question, kind="escalation", query=row)
        notify_query(row)
    return {"pending": True, "query_id": row.pk, "status": row.status,
        "instruction": "The responsible user has been notified. Do not ask the requesting user to supply the other party's facts or invent an answer."}


@transaction.atomic
def answer_query(query_id, account, answer, scope="private"):
    row = AgentQuery.objects.select_for_update().select_related("conversation__student", "conversation__university").get(pk=query_id)
    conv = row.conversation
    allowed = ((row.direction == AgentQuery.Direction.STUDENT and account.role == "university" and account.university_id == conv.university_id)
        or (row.direction == AgentQuery.Direction.UNIVERSITY and account.role == "student" and account.student_profile_id == conv.student_id))
    if not allowed:
        raise PermissionDenied("Only the requested party can answer this query.")
    if row.status == "answered":
        raise ValidationError("This query has already been answered.")
    if scope not in ("private", "university") or (scope == "university" and account.role != "university"):
        raise ValidationError("Invalid answer scope.")
    answer = answer.strip()
    if not answer or len(answer) > 12000:
        raise ValidationError("Provide an answer of 1â€“12000 characters.")
    row.answer, row.answer_scope, row.status = answer, scope, "answered"
    row.answered_by, row.answered_at = account.user, timezone.now()
    # Publishing is an explicit officer choice. Individual student cases never enter shared KB automatically.
    if scope == "university":
        row.knowledge_entry = UniversityKnowledgeEntry.objects.create(university_id=str(conv.university.uuid),
            topic=row.question[:500], content=answer, source_type="human_verified", confidence=1,
            details={"agent_query_id": row.pk, "answered_by": str(account.user_id), "question": row.question})
    row.save()
    actor = "university" if account.role == "university" else "student"
    message(conv, actor, answer, kind="human_answer", query=row)
    # This is the exact human answer relayed by the receiving agent, never a fabricated model response.
    message(conv, actor + "_agent", answer, kind="reply", query=row, metadata={"source": "human_answer", "answer_scope": scope})
    notify_query(row, answered=True)
    return row
