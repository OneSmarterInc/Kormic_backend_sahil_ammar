# pure_multi_agent/tasks.py
# Background side-effects for agent-turn failures: alerting ops (off the
# request path, so a slow/unreachable SMTP server never adds to a student's
# already-failed turn) and probing for recovery.
from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task

logger = logging.getLogger(__name__)


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

    from pure_multi_agent.student_graph import _get_model

    try:
        _get_model().invoke([HumanMessage(content="ping")])
    except Exception as exc:
        logger.info("check_agent_recovery_task: agent still unavailable (%s)", exc)
        return

    notify_agent_recovered()
