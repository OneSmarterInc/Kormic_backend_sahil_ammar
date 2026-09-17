# extra_utils/send_mail_to_superuser.py
# Ops alerting for the student-agent chat pipeline (pure_multi_agent.runtime).
# Two things live here: emailing settings.AGENT_ALERT_EMAILS when the agent
# starts failing (e.g. Anthropic credit exhaustion, as actually happened --
# students saw a raw "check your ANTHROPIC_API_KEY" message with no one
# aware it was even failing), and emailing again once
# pure_multi_agent.tasks.check_agent_recovery_task confirms it's answering
# again. Students themselves never see any of this -- see
# pure_multi_agent.runtime.run_turn's generic fallback reply.
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

# Presence of this key (not its value) means "an outage alert has already
# gone out and hasn't been cleared by a successful recovery probe yet".
# Stored in the shared Redis cache (settings.CACHES) so it's correct across
# every gunicorn worker -- not a per-process flag -- and cache.add's
# atomicity means concurrent failures across workers still only alert once.
AGENT_OUTAGE_CACHE_KEY = "pure_multi_agent:llm_outage"


def _alert_recipients() -> List[str]:
    recipients = [addr.strip() for addr in getattr(settings, "AGENT_ALERT_EMAILS", []) if addr.strip()]
    if not recipients:
        logger.warning("AGENT_ALERT_EMAILS is empty -- no one will be notified of agent outages.")
    return recipients


def notify_agent_error(error_text: str, *, student_id: Optional[str] = None) -> None:
    """
    Alert ops the first time a student's agent turn fails, not on every
    failure -- a real outage (credits/network/model access) fails every
    student's next message too, and that shouldn't become one email per
    message. cache.add only returns True for whichever caller actually sets
    the key, so this stays "alert once per outage" even under concurrent
    requests across workers.
    """
    is_new_outage = cache.add(AGENT_OUTAGE_CACHE_KEY, True, timeout=None)
    if not is_new_outage:
        return

    recipients = _alert_recipients()
    if not recipients:
        return

    subject = "[Kormic] Student agent is failing -- action needed"
    body = (
        "The student chat agent just failed to generate a response.\n\n"
        f"Time (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}\n"
        f"Student: {student_id or 'unknown'}\n"
        f"Error: {error_text}\n\n"
        "Students are currently seeing a generic 'please try again later' "
        "message instead of this detail -- nothing internal is exposed to "
        "them. A background check runs automatically and will email this "
        "same list again as soon as the agent is answering normally, so no "
        "action is needed here beyond fixing the underlying cause (e.g. "
        "Anthropic billing/credits, network access, or model access)."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception:
        logger.exception("Failed to send agent-outage alert email to %s", recipients)


def notify_agent_recovered() -> None:
    """Called by check_agent_recovery_task once a probe call succeeds after
    an outage. Clears the outage flag (so a later failure alerts again) and
    tells the same recipients it's safe to expect real answers again."""
    cache.delete(AGENT_OUTAGE_CACHE_KEY)

    recipients = _alert_recipients()
    if not recipients:
        return

    subject = "[Kormic] Student agent has recovered"
    body = (
        "The student chat agent is answering normally again as of "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC.\n\n"
        "No further action needed."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception:
        logger.exception("Failed to send agent-recovery email to %s", recipients)
