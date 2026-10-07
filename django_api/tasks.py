import logging
from typing import Any, Dict

from celery import shared_task

from django_api.models import AgentAuditLog

logger = logging.getLogger(__name__)


@shared_task
def save_audit_log_task(
    run_id: str,
    student_id: str,
    actor_agent: str,
    action_type: str,
    target: str,
    inputs: Dict[str, Any],
    outputs: Dict[str, Any]
):
    """
    Asynchronously save an agent audit log entry so the student's chat request
    isn't blocked by telemetry writes.
    """
    try:
        AgentAuditLog.objects.create(
            run_id=run_id,
            student_id=student_id,
            actor_agent=actor_agent,
            action_type=action_type,
            target=target,
            inputs=inputs,
            outputs=outputs
        )
    except Exception as e:
        logger.error(f"Failed to save agent audit log: {e}")


@shared_task
def cleanup_old_audit_logs_task():
    """Legacy task name retained for queued messages; audit purge is disabled."""
    logger.warning("Agent audit deletion is disabled pending an approved audit/legal retention policy.")
    return 0
