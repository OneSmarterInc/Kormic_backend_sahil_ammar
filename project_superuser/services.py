from __future__ import annotations

"""
Helpers backing project_superuser.views (and, via log_activity, accounts.
views): purge_* clean up the rows in django_api that only reference
student_id/university_id as a loose string (Django can't cascade those off
a deleted auth.User the way it does Account/TOTPDevice/TOTPBackupCode/
GitHubOAuthConnection); revoke_all_sessions backs the force-logout action;
log_activity writes every ActivityLog entry -- both the admin-initiated
ones (TOTP reset/password reset/session revoke) and the self-service auth
ones (register/login/logout) that accounts.views logs on the same model.
"""


def _purge_agent_identity(owner_type: str, owner_id: str) -> None:
    """AgentIdentity (A3) is owner_type/owner_id, not a real FK to
    StudentProfile/University, so it needs the same manual purge as the
    other loose-string-keyed rows in this module. Deleting the identity
    cascades its AgentConversationLog rows (both asker and responder FKs)."""
    from django_api.models import AgentIdentity

    AgentIdentity.objects.filter(owner_type=owner_type, owner_id=owner_id).delete()


class StudentWorkInProgress(Exception):
    """Deletion must not race a worker that can still write student data."""


def purge_student_data(student_id: str) -> None:
    from django.db import transaction
    from django_api.models import (
        AgentAuditLog, AgentIdentity, AgentJob, AriaMemory, ChatMessage,
        IntakeSession, PendingQuery, StudentProfile,
    )
    from agent_queries.models import AgentActivity

    with transaction.atomic():
        # Admission and worker claiming lock this same row. A worker that has
        # already started must finish before we promise permanent deletion.
        StudentProfile.objects.select_for_update().get(uuid=student_id)
        if AgentJob.objects.filter(student_id=student_id, status='processing').exists():
            raise StudentWorkInProgress('This student has active processing. Retry deletion after it finishes.')
        from pure_multi_agent.runtime import reset_conversation
        reset_conversation(student_id)
        AgentJob.objects.filter(student_id=student_id).delete()
        AgentAuditLog.objects.filter(student_id=student_id).delete()
        PendingQuery.objects.filter(student_id=student_id).delete()
        AgentActivity.objects.filter(owner_key='student:' + str(student_id)).delete()

        # Real foreign keys cascade documents, GitHub checkpoints and queries.
        StudentProfile.objects.filter(uuid=student_id).delete()
        AriaMemory.objects.filter(student_id=student_id).delete()
        IntakeSession.objects.filter(student_id=student_id).delete()
        ChatMessage.objects.filter(student_id=student_id).delete()
        _purge_agent_identity(AgentIdentity.OwnerType.STUDENT, student_id)
        transaction.on_commit(lambda: _purge_student_files(student_id))


def _purge_student_files(student_id: str) -> None:
    """Delete only the exact UUID-owned folders under the upload roots."""
    from pathlib import Path
    from uuid import UUID
    import shutil
    from django.conf import settings
    sid = str(UUID(str(student_id)))
    for root in {Path(settings.MEDIA_ROOT).resolve(), (settings.BASE_DIR / 'uploads').resolve()}:
        for folder in ('resumes', 'linkedin', 'chat_attachments', 'queued-documents', 'profile_images'):
            target = (root / folder / sid).resolve()
            if target.is_relative_to(root) and target.name == sid and target.is_dir():
                shutil.rmtree(target)


def purge_university_data(university_id: str) -> None:
    from django_api.models import (
        AgentIdentity,
        ChatMessage,
        PendingQuery,
        PresenterAuditLog,
        UniversityKnowledgeEntry,
        UniversityQuestionLog,
        VerifiedAnswer,
    )

    UniversityKnowledgeEntry.objects.filter(university_id=university_id).delete()
    PendingQuery.objects.filter(university_id=university_id).delete()
    VerifiedAnswer.objects.filter(university_id=university_id).delete()
    UniversityQuestionLog.objects.filter(university_id=university_id).delete()
    PresenterAuditLog.objects.filter(university_id=university_id).delete()
    ChatMessage.objects.filter(university_id=university_id).delete()
    _purge_agent_identity(AgentIdentity.OwnerType.UNIVERSITY, university_id)
    # KnowledgeGroup cascades off University itself (real FK) -- no manual
    # cleanup needed here.


def revoke_all_sessions(user) -> int:
    """
    Blacklists every not-yet-blacklisted outstanding refresh token for
    `user`. Relies on rest_framework_simplejwt.token_blacklist (already
    installed) recording an OutstandingToken row each time a RefreshToken
    is issued for a user. Returns how many tokens were newly blacklisted.
    """
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    from accounts.models import Account
    from django.db.models import F

    Account.objects.filter(user=user).update(session_version=F("session_version") + 1)
    tokens = OutstandingToken.objects.filter(user=user).exclude(blacklistedtoken__isnull=False)
    count = 0
    for token in tokens:
        BlacklistedToken.objects.get_or_create(token=token)
        count += 1
    return count


def escalation_metrics(university_id: str = "", weeks: int = 12) -> dict:
    """
    A4: the Fall-pilot headline number -- escalations per student per week,
    broken down by knowledge group, over time. Reads PendingQuery directly
    (every escalation is already a durable, queryable row); nothing new is
    persisted for this. university_id="" combines every university in the
    pilot into one series; weeks is clamped to [1, 52].
    """
    from django.db.models import Count, Q
    from django.db.models.functions import TruncWeek
    from django.utils import timezone

    from django_api.models import PendingQuery

    weeks = max(1, min(int(weeks or 12), 52))
    since = timezone.now() - timezone.timedelta(weeks=weeks)

    queryset = PendingQuery.objects.filter(created_at__gte=since)
    if university_id:
        queryset = queryset.filter(university_id=university_id)

    weekly_rows = (
        queryset.annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(
            total_escalations=Count("id"),
            # A blank student_id (no student context on the call) can't be
            # attributed to a student-week rate, so it's excluded from the
            # denominator but still counted in total_escalations above.
            distinct_students=Count("student_id", filter=~Q(student_id=""), distinct=True),
        )
        .order_by("week")
    )

    group_rows = (
        queryset.annotate(week=TruncWeek("created_at"))
        .exclude(group__isnull=True)
        .values("week", "group__slug")
        .annotate(count=Count("id"))
    )
    by_group_per_week: dict = {}
    for row in group_rows:
        by_group_per_week.setdefault(row["week"], {})[row["group__slug"]] = row["count"]

    weeks_out = []
    for row in weekly_rows:
        total = row["total_escalations"]
        distinct_students = row["distinct_students"]
        weeks_out.append({
            "week_start": row["week"].date().isoformat() if row["week"] else None,
            "total_escalations": total,
            "distinct_students": distinct_students,
            "escalations_per_student": round(total / distinct_students, 2) if distinct_students else 0.0,
            "by_group": by_group_per_week.get(row["week"], {}),
        })

    return {
        "university_id": university_id or "all",
        "weeks_requested": weeks,
        "weeks": weeks_out,
    }


def log_activity(
    action: str,
    *,
    actor=None,
    target_user=None,
    target_email: str = "",
    target_role: str = "",
    target_student_id: str = "",
    target_university_id: str = "",
) -> None:
    """
    Writes one ActivityLog row. `actor` is who performed the action (None
    for a failed login, since nothing about the caller is proven yet);
    `target_user` is whose account it happened to (None for a failed
    login against an email with no matching account -- pass the attempted
    email as `target_email` in that case). `target_role`/`target_student_id`/
    `target_university_id` are derived from `target_user.account` when
    omitted and a target_user is given -- letting a caller filter/group
    entries by student or university without a join back to Account.
    """
    from project_superuser.models import ActivityLog

    if target_user is not None:
        target_email = target_email or target_user.email
        if not (target_role or target_student_id or target_university_id):
            account = getattr(target_user, "account", None)
            if account:
                target_role = account.role
                target_student_id = account.student_uuid or ""
                target_university_id = account.university_uuid or ""

    ActivityLog.objects.create(
        actor=actor,
        actor_email=actor.email if actor else "",
        action=action,
        target_user=target_user,
        target_email=target_email,
        target_role=target_role,
        target_student_id=target_student_id,
        target_university_id=target_university_id,
    )
