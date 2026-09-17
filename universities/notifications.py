# universities/notifications.py
# Two escalation emails to a knowledge group's contact:
#   send_escalation_routed_alert  -- fired automatically the moment an
#       escalation is routed to the group (from the agent's escalation path).
#       Best-effort: never raises, so a mail failure can't block escalation
#       creation.
#   send_escalation_digest_email  -- the officer-triggered "notify" button.
#       Here the caller explicitly clicked and needs to know whether the
#       email went out, so this one reports success/failure instead.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from django.conf import settings
from django.core.mail import send_mail

if TYPE_CHECKING:
    from django_api.models import PendingQuery

logger = logging.getLogger(__name__)


def send_escalation_routed_alert(*, query: "PendingQuery", group) -> bool:
    """Email a knowledge group's configured contact the instant a single
    escalation is routed to that group. Called from the agent escalation
    path (agents.university_agent.create_pending_query), so it must never
    raise -- any failure is logged and swallowed. No-ops (returns False) if
    the group has no contact email set. Returns True on a successful send."""
    if group is None or not getattr(group, "escalation_contact_email", ""):
        return False

    to_email = group.escalation_contact_email
    group_label = group.get_slug_display()
    to_name = group.escalation_contact_name or group_label
    university_name = query.university_name or "your university"

    subject = f"[{university_name}] New escalation routed to {group_label}"

    lines = [
        f"Hi {to_name},",
        "",
        f"A student question has just been routed to {group_label} for "
        f"{university_name} and needs a response:",
        "",
        f"- [{(query.priority or '').upper()}] {query.student_name or 'A student'}: {query.question}",
    ]
    if query.program:
        lines.append(f"  Program: {query.program}")
    if query.urgency_reason:
        lines.append(f"  Reason flagged urgent: {query.urgency_reason}")
    lines += [
        "",
        "Open the officer dashboard to review and answer it.",
        "",
        f"-- {university_name} (via Kormic)",
    ]
    message = "\n".join(lines)

    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send escalation-routed alert to %s", to_email)
        return False


def send_escalation_digest_email(
    *,
    to_email: str,
    to_name: str,
    group_label: str,
    university_name: str,
    escalations: Iterable["PendingQuery"],
    custom_message: str = "",
) -> bool:
    """Email a knowledge group's escalation contact a digest of the student
    questions routed to them. Returns True if the send succeeded."""
    escalations = list(escalations)
    subject = f"[{university_name}] {len(escalations)} escalation(s) routed to {group_label}"

    lines = [f"Hi {to_name or group_label},", ""]
    if custom_message:
        lines += [custom_message, ""]
    lines.append(
        f"The following student question(s) have been routed to {group_label} "
        f"for {university_name} and need a response:"
    )
    lines.append("")

    for query in escalations:
        lines.append(f"- [{(query.priority or '').upper()}] {query.student_name or 'A student'}: {query.question}")
        if query.urgency_reason:
            lines.append(f"  Reason flagged urgent: {query.urgency_reason}")
        if query.created_at:
            lines.append(f"  Submitted: {query.created_at:%Y-%m-%d %H:%M}")
        lines.append("")

    lines.append(f"-- {university_name} (via Kormic)")
    message = "\n".join(lines)

    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send escalation digest email to %s", to_email)
        return False
