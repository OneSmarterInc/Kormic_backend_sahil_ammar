"""Conservative, bounded retention for duplicate agent execution state.

The visible transcript, profile artefacts, unresolved queries, face data and
student LangGraph conversation checkpoints are deliberately outside this job.
"""
from contextlib import nullcontext
from datetime import date, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django_api.models import AgentJob, ChatMessage, DataRetentionHold, GitHubAgentCheckpoint


def restore_test_is_recent(backup_id, tested_at, *, today=None):
    """Require a dated, recent operator restore attestation before any purge."""
    if not backup_id or not tested_at:
        return False
    try:
        tested = date.fromisoformat(tested_at)
    except (TypeError, ValueError):
        return False
    age = (today or timezone.localdate()) - tested
    return timedelta(0) <= age <= timedelta(days=30)


def _held(student_id):
    keys = ["*"]
    if student_id:
        keys.append(f"student:{student_id}")
    return DataRetentionHold.objects.filter(subject_key__in=keys, released_at__isnull=True).exists()


def _compactable(job, cutoff):
    if (job.status != "completed" or job.kind not in ("student", "student_edit")
            or not job.completed_at or job.completed_at >= cutoff
            or job.retention_checked_at or job.retention_compacted_at or _held(job.student_id)):
        return False
    payload, result = job.payload or {}, job.result or {}
    if (not isinstance(payload, dict) or not isinstance(result, dict)
            or payload.get("university_cache_pending") or result.get("pending")
            or not isinstance(result.get("reply"), str)
            or not isinstance(payload.get("assistant_message_id"), int)
            or not isinstance(payload.get("message_id"), int)
            or result.get("message_id") != payload["message_id"]):
        return False
    return ChatMessage.objects.filter(
        Q(pk=payload["message_id"], sender="user") |
        Q(pk=payload["assistant_message_id"], sender="assistant", content=result["reply"]),
        student_id=job.student_id, channel="agent",
    ).count() == 2


def _compact_job(job, now):
    payload = job.payload
    result = job.result
    # A follow-up can recover a single university candidate from older jobs.
    # Keep this tiny field and the message IDs; drop prompts, tool state and the
    # duplicate result body, which remains in the visible ChatMessage row.
    state = payload.get("resume_state") or {}
    compact_payload = {
        "message_id": payload["message_id"],
        "assistant_message_id": payload["assistant_message_id"],
    }
    if isinstance(state, dict) and state.get("university_candidates"):
        compact_payload["resume_state"] = {"university_candidates": state["university_candidates"]}
    job.payload = compact_payload
    job.result = {key: result[key] for key in ("agent", "student_id", "message_id", "pending", "query_id") if key in result}
    job.result["archived_to_history"] = True
    job.retention_checked_at = now
    job.retention_compacted_at = now
    job.save(update_fields=["payload", "result", "retention_checked_at", "retention_compacted_at"])


def run_retention(*, apply=False, max_rows=500, now=None):
    """Plan or execute a bounded pass. Approval/restore gating is caller-owned."""
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    now = now or timezone.now()
    job_days = settings.AGENT_RESULT_RETENTION_DAYS
    checkpoint_days = settings.GITHUB_CHECKPOINT_RETENTION_DAYS
    if job_days < 1 or checkpoint_days < 1:
        raise ValueError("retention windows must be positive")
    job_cutoff = now - timedelta(days=job_days)
    checkpoint_cutoff = now - timedelta(days=checkpoint_days)
    counts = {"jobs_compacted": 0, "jobs_skipped": 0, "github_checkpoints_deleted": 0}
    held_keys = list(DataRetentionHold.objects.filter(released_at__isnull=True).values_list("subject_key", flat=True))
    if "*" in held_keys:
        return counts
    held_students = [key.removeprefix("student:") for key in held_keys if key.startswith("student:")]
    held_uuids = []
    for value in held_students:
        try:
            held_uuids.append(UUID(value))
        except ValueError:
            continue

    job_ids = AgentJob.objects.filter(
        status="completed", kind__in=("student", "student_edit"),
        completed_at__lt=job_cutoff, retention_checked_at__isnull=True,
        payload__has_key="assistant_message_id", result__pending=False,
    ).exclude(student_id__in=held_students).order_by("completed_at", "pk").values_list("pk", flat=True)[:max_rows]
    for pk in list(job_ids):
        with transaction.atomic() if apply else nullcontext():
            jobs = AgentJob.objects.select_for_update() if apply else AgentJob.objects
            job = jobs.filter(pk=pk).first()
            if job is None:
                continue
            if not _compactable(job, job_cutoff):
                counts["jobs_skipped"] += 1
                if apply and not job.retention_checked_at:
                    job.retention_checked_at = now
                    job.save(update_fields=["retention_checked_at"])
                continue
            counts["jobs_compacted"] += 1
            if apply:
                _compact_job(job, now)

    checkpoint_ids = GitHubAgentCheckpoint.objects.filter(
        run__status__in=("completed", "failed"), run__updated_at__lt=checkpoint_cutoff,
        run__lease_token__isnull=True,
    ).exclude(run__profile__student__uuid__in=held_uuids).order_by("pk").values_list("pk", flat=True)[:max_rows]
    for pk in list(checkpoint_ids):
        with transaction.atomic() if apply else nullcontext():
            checkpoints = GitHubAgentCheckpoint.objects.select_for_update() if apply else GitHubAgentCheckpoint.objects
            checkpoint = checkpoints.select_related("run__profile__student").filter(pk=pk).first()
            if checkpoint is None:
                continue
            run = checkpoint.run
            if (run.status not in ("completed", "failed") or run.updated_at >= checkpoint_cutoff
                    or run.lease_token is not None
                    or _held(run.profile.student.uuid)):
                continue
            counts["github_checkpoints_deleted"] += 1
            if apply:
                checkpoint.delete()  # Cascades only this checkpoint's pending writes.
    return counts
