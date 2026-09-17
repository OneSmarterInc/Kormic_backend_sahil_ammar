"""
Single entry point for "tell the student about X" -- both a push notification
and, where relevant, the corresponding chat message. Every trigger (a live
agent reply, a resolved pending query, a proactive agent-initiated message,
or any future event) should go through send_agent_message()/notify_*() here
rather than calling Expo or Celery directly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from accounts.models import Account
from notifications.models import NotificationLog
from notifications.tasks import send_push_notification_task

PREVIEW_LENGTH = 120


def _truncate(text: str, limit: int = PREVIEW_LENGTH) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _queue_push(
    *,
    account: Account,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    queue_push: bool = True,
) -> NotificationLog:
    log = NotificationLog.objects.create(
        account=account,
        event_type=event_type,
        title=title,
        body=body,
        data=data or {},
    )
    if queue_push:
        send_push_notification_task.delay(log.id)
    return log


def notify_agent_reply(*, student_id: str, agent_name: str, reply: str) -> Optional[NotificationLog]:
    """
    Fire after a live agent_chat turn. The ChatMessage rows for this turn are
    already written by log_chat_turn() in django_api/views.py -- this only
    queues the push, so the student is notified even if they backgrounded or
    closed the app while the reply was being generated.
    """
    from django_api.services import as_uuid

    if not as_uuid(student_id):
        return None
    account = Account.objects.filter(student_profile__uuid=student_id).first()
    if account is None:
        return None

    return _queue_push(
        account=account,
        event_type=NotificationLog.EventType.AGENT_REPLY,
        title=f"{agent_name or 'Your agent'} replied",
        body=_truncate(reply) or "New message from your agent.",
        data={"type": "agent_reply"},
    )


def send_agent_message(
    *,
    student_id: str,
    content: str,
    event_type: str = NotificationLog.EventType.AGENT_INITIATED,
    title: str = "New message from your agent",
    meta: Optional[Dict[str, Any]] = None,
    notification_data: Optional[Dict[str, Any]] = None,
    queue_push: bool = True,
) -> Optional[NotificationLog]:
    """
    General-purpose hook for any message the agent (or an operator/automation
    acting through it) needs to deliver outside of a live chat turn: a
    resolved pending query, a proactive check-in, a scheduled nudge, an admin
    broadcast, etc. Writes the message into the student's agent chat thread
    *and* queues the push, so it "just shows up" in chat whether or not the
    student sees the notification.

    queue_push=False skips the individual Celery dispatch and leaves the
    NotificationLog as PENDING -- used by callers that batch many students'
    pushes into one Expo call themselves (see
    notifications.tasks.run_proactive_checkin_batch_task).
    """
    from django_api.services import as_uuid

    if not as_uuid(student_id):
        return None
    account = Account.objects.filter(student_profile__uuid=student_id).first()
    if account is None:
        return None

    from django_api.models import ChatMessage

    ChatMessage.objects.create(
        channel=ChatMessage.Channel.AGENT,
        student_id=student_id,
        sender=ChatMessage.Sender.ASSISTANT,
        content=content,
        meta=meta or {},
    )

    return _queue_push(
        account=account,
        event_type=event_type,
        title=title,
        body=_truncate(content),
        data=notification_data or {"type": event_type},
        queue_push=queue_push,
    )


def notify_pending_query_resolved(
    *,
    student_id: str,
    university_id: str,
    question: str,
    answer: str,
    query_id: int,
) -> Optional[NotificationLog]:
    question = (question or "").strip()
    answer = (answer or "").strip()
    content = f'Q{query_id}: "{question}"\n\n{answer}'
    return send_agent_message(
        student_id=student_id,
        content=content,
        event_type=NotificationLog.EventType.PENDING_QUERY_RESOLVED,
        title="Your question has been answered",
        meta={
            "type": "pending_query_resolved",
            "query_id": query_id,
            "question": question,
            "answer": answer,
            "university_id": university_id,
        },
        notification_data={"type": "pending_query_resolved", "query_id": query_id},
    )
