import logging
from datetime import timedelta
from typing import Any, Dict

from celery import shared_task
from django.utils import timezone

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
    """
    Runs nightly via Celery beat to delete AgentAuditLog entries older than 90 days,
    keeping the PostgreSQL table size bounded without requiring cold storage.
    """
    try:
        cutoff_date = timezone.now() - timedelta(days=90)
        deleted_count, _ = AgentAuditLog.objects.filter(timestamp__lt=cutoff_date).delete()
        logger.info(f"Cleaned up {deleted_count} old agent audit logs.")
    except Exception as e:
        logger.error(f"Failed to clean up old audit logs: {e}")



# Register the dedicated generation task with Celery autodiscovery.
from django_api.chat_tasks import generate_chat  # noqa: F401,E402
