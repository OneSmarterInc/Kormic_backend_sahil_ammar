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


def notify_account(
    *,
    account: Account,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    queue_push: bool = False,
    queue_email: bool = False,
) -> NotificationLog:
    """Create one durable, account-scoped notification.

    Web roles consume this through the notification inbox API. Student
    notifications may additionally set queue_push=True to preserve native
    Expo delivery. Account ownership is resolved server-side; clients never
    choose another account's notification scope.
    """
    log = NotificationLog.objects.create(
        account=account,
        event_type=event_type,
        title=title,
        body=_truncate(body),
        data=data or {},
    )
    if queue_email and account.user.is_active and account.user.email:
        from notifications.models import EmailDelivery
        EmailDelivery.objects.create(notification=log)
    if queue_push:
        from django.conf import settings
        if settings.AGENT_QUEUE_BACKEND == 'database':
            from notifications.models import PushDelivery
            PushDelivery.objects.create(notification=log)
        else:
            send_push_notification_task.delay(log.id)
    return log


def _queue_push(
    *,
    account: Account,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    queue_push: bool = True,
) -> NotificationLog:
    return notify_account(
        account=account,
        event_type=event_type,
        title=title,
        body=body,
        data=data,
        queue_push=queue_push,
    )


def notify_university(
    university_id: str,
    *,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    queue_email: bool = False,
    group_id: int | None = None,
) -> list[NotificationLog]:
    logs = []
    from django.db.models import Q
    recipients = Q(role=Account.Role.UNIVERSITY)
    if group_id:
        recipients |= Q(role=Account.Role.DEPARTMENT, departments__pk=group_id)
    for account in Account.objects.filter(recipients, university__uuid=university_id, user__is_active=True).select_related('user').distinct():
        logs.append(notify_account(
            account=account,
            event_type=event_type,
            title=title,
            body=body,
            data=data,
            queue_email=queue_email,
        ))
    return logs


def notify_institute(
    institute_id: str,
    *,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> list[NotificationLog]:
    logs = []
    for account in Account.objects.filter(
        role=Account.Role.INSTITUTE,
        institute__uuid=institute_id,
        user__is_active=True,
    ):
        logs.append(notify_account(
            account=account,
            event_type=event_type,
            title=title,
            body=body,
            data=data,
            queue_email=event_type == NotificationLog.EventType.STUDENT_CLAIMED,
        ))
    return logs


def notify_superusers(
    *,
    event_type: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    queue_email: bool = False,
) -> list[NotificationLog]:
    logs = []
    for account in Account.objects.filter(role=Account.Role.SUPERUSER, user__is_active=True).select_related('user'):
        logs.append(notify_account(
            account=account,
            event_type=event_type,
            title=title,
            body=body,
            data=data,
            queue_email=queue_email,
        ))
    return logs


def notify_agent_reply(*, student_id: str, agent_name: str, reply: str) -> Optional[NotificationLog]:
    """Ordinary chatbot replies stay in chat without creating notifications.

    Keep the entry point compatible with existing chat workers. Human query
    answers use a separate notification path and remain enabled.
    """
    return None


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
    from django_api.services import as_uuid
    if not as_uuid(student_id):
        return
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


def notify_profile_processed(student_id, source, job_id):
    """Call inside the fenced completion transaction, once per successful job."""
    from notifications.models import PushDelivery
    from django_api.services import as_uuid
    if not as_uuid(student_id):
        return
    account = Account.objects.filter(student_profile__uuid=student_id).first()
    if account is None:
        return
    label = {'resume': 'CV', 'linkedin': 'LinkedIn', 'github': 'GitHub'}[source]
    log = notify_account(account=account, event_type='profile_processed',
        title=f'{label} processing complete', body=f'Your {label} information is ready to review in your profile.',
        data={'type': 'profile_processed', 'source': source, 'job_id': str(job_id)})
    PushDelivery.objects.create(notification=log)
