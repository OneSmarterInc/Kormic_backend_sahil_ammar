"""Scheduled privacy maintenance. Failed erasure stays visible and retryable."""
import os
from datetime import timedelta
from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_api import models as m
from accounts.privacy import safe_path, files_for_student, queue_deletion


def revoke_github(user):
    import requests
    from accounts.models import GitHubOAuthConnection
    from accounts.crypto import decrypt_secret
    grant = GitHubOAuthConnection.objects.filter(user=user).first()
    if not grant: return
    client_id, secret = os.getenv("GITHUB_OAUTH_CLIENT_ID"), os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
    if not client_id or not secret: raise RuntimeError("OAUTH_REVOCATION_NOT_CONFIGURED")
    response = requests.delete(f"https://api.github.com/applications/{client_id}/grant",
        auth=(client_id, secret), json={"access_token": decrypt_secret(grant.access_token_encrypted)}, timeout=(3, 10))
    if response.status_code not in {204, 404}: raise RuntimeError("OAUTH_REVOCATION_FAILED")
    grant.delete()


def remove_file(path):
    if path: safe_path(path).unlink(missing_ok=True)


def erase_student(job):
    user = job.user
    if user is None: return
    profile = user.account.student_profile
    sid = job.student_id
    revoke_github(user)
    if sid:
        from pure_multi_agent.runtime import reset_conversation
        reset_conversation(sid)  # Failure prevents a false claim of complete erasure.
    if profile:
        # The per-student upload directory also contains superseded/orphaned uploads.
        for value in files_for_student(profile): remove_file(value)
        from django_api.services import UPLOADS_DIR
        for folder in (UPLOADS_DIR.iterdir() if UPLOADS_DIR.exists() else []):
            if not folder.is_dir(): continue
            directory = folder / sid
            if directory.exists():
                directory = safe_path(str(directory))
                import shutil
                shutil.rmtree(directory)
    from institutes_list.models import ListedStudent, UniversityStudentList
    from project_superuser.models import ActivityLog
    # Verbatim roster files cannot be selectively redacted safely. Remove the
    # file containing this student's row; retain other students' structured rows.
    roster_rows = ListedStudent.objects.filter(Q(claimed_student_id=sid) | Q(email__iexact=user.email)) if sid else ListedStudent.objects.filter(email__iexact=user.email)
    for source in UniversityStudentList.objects.filter(students__in=roster_rows).distinct():
        remove_file(source.source_file_path)
        source.source_file_path = source.source_file_name = source.source_file_content_type = ""
        source.source_file_size = 0
        source.save(update_fields=["source_file_path", "source_file_name", "source_file_content_type", "source_file_size"])
    roster_rows.delete()
    ActivityLog.objects.filter(Q(actor=user) | Q(target_user=user) | Q(actor_email=user.email) | Q(target_email=user.email)).delete()
    if sid:
        # Human answers to the student's private question may themselves contain PII.
        for query in m.PendingQuery.objects.filter(student_id=sid):
            m.UniversityKnowledgeEntry.objects.filter(university_id=query.university_id, topic=query.question, source_type__in=["human_verified", "verified"]).delete()
        m.VerifiedAnswer.objects.filter(query__student_id=sid).delete()
        m.PendingQuery.objects.filter(student_id=sid).delete()
        m.AgentIdentity.objects.filter(owner_type="student", owner_id=sid).delete()
        m.AgentAuditLog.objects.filter(student_id=sid).delete()
        m.ChatMessage.objects.filter(student_id=sid).delete()
        m.ChatGeneration.objects.filter(student_id=sid).delete()
        m.ChatGate.objects.filter(key="student:" + sid).delete()
        m.AriaMemory.objects.filter(student_id=sid).delete()
        m.IntakeSession.objects.filter(Q(student_id=sid) | Q(student_key=sid)).delete()
        # Old presenter logs used names instead of IDs: conservatively redact
        # matching entries rather than retaining an unresolvable identifier.
        if profile and profile.name:
            m.UniversityQuestionLog.objects.filter(student_name=profile.name).delete()
            m.PresenterAuditLog.objects.filter(profile_name=profile.name).delete()
    m.ChatModelCall.objects.filter(account_id=user.account.pk).delete()
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
    OutstandingToken.objects.filter(user=user).delete()
    if profile: profile.delete()
    user.delete()


@shared_task(soft_time_limit=240, time_limit=270, ignore_result=True)
def process_deletions():
    ids = list(m.StudentDeletion.objects.exclude(status="completed").filter(not_before__lte=timezone.now()).values_list("pk", flat=True)[:20])
    for pk in ids:
        try:
            with transaction.atomic():
                job = m.StudentDeletion.objects.select_for_update().get(pk=pk)
                if job.status == "completed": continue
                erase_student(job)
                job.user = None
                job.status, job.student_id, job.error_code = "completed", "", ""
                job.completed_at = timezone.now()
                job.save(update_fields=["status", "student_id", "error_code", "completed_at"])
        except Exception:
            m.StudentDeletion.objects.filter(pk=pk).update(status="retry_pending", error_code="ERASURE_RETRY_REQUIRED", not_before=timezone.now() + timedelta(hours=1))


def expire_rows(queryset, file_field=None):
    for row in queryset.iterator(chunk_size=100):
        if file_field:
            value = getattr(row, file_field)
            for path in value if isinstance(value, list) else [value]: remove_file(path)
        row.delete()


@shared_task(soft_time_limit=600, time_limit=660, ignore_result=True)
def apply_retention():
    from accounts.models import Account
    from notifications.models import PushToken, NotificationLog
    from verification.models import VerificationCheck
    from institutes_list.models import UniversityStudentList
    from universities.models import University
    policy, _ = m.RetentionPolicy.objects.get_or_create(scope="global")
    now = timezone.now()
    before = lambda days: now - timedelta(days=days)
    # Whole inactive accounts include OAuth grants and all derived data.
    # Only accounts with measured activity are eligible. Legacy accounts start
    # their retention clock on their next authenticated request, never from an
    # unreliable JWT last_login timestamp.
    for account in Account.objects.filter(role="student", user__is_active=True,
            last_active_at__lt=before(policy.inactive_account_days)).select_related("user")[:100]:
        queue_deletion(account.user)
    affected = set(m.ResumeUpload.objects.filter(created_at__lt=before(policy.upload_days)).values_list("student_id", flat=True))
    affected.update(m.LinkedInAnalysis.objects.filter(created_at__lt=before(policy.upload_days)).values_list("student_id", flat=True))
    expire_rows(m.ResumeUpload.objects.filter(created_at__lt=before(policy.upload_days)), "file_path")
    expire_rows(m.LinkedInAnalysis.objects.filter(created_at__lt=before(policy.upload_days)), "image_paths")
    # Derived verification cannot continue certifying evidence that was removed.
    expired_checks = VerificationCheck.objects.filter(Q(student_id__in=affected) | Q(updated_at__lt=before(policy.verification_days)))
    m.StudentProfile.objects.filter(pk__in=expired_checks.values("student_id")).update(verified=False)
    expired_checks.delete()
    for profile in m.StudentProfile.objects.exclude(profile_image_path="").filter(updated_at__lt=before(policy.upload_days)):
        remove_file(profile.profile_image_path); profile.profile_image_path = ""; profile.save(update_fields=["profile_image_path"])
    from pure_multi_agent.runtime import reset_conversation
    # Checkpoints contain complete conversation history. Reset derived memory
    # whenever a transcript expires, even for a currently active account.
    for profile in m.StudentProfile.objects.all().iterator():
        sid = str(profile.uuid)
        with transaction.atomic():
            m.ChatGate.objects.get_or_create(key="student:" + sid)
            m.ChatGate.objects.select_for_update().get(key="student:" + sid)
            if m.ChatGeneration.objects.filter(student_id=sid, status__in=["queued", "running"]).exists(): continue
            stale = m.ChatMessage.objects.filter(student_id=sid, created_at__lt=before(min(policy.memory_days, policy.transcript_days)))
            if not stale.exists() and profile.memory_reset_at >= before(policy.memory_days): continue
            reset_conversation(sid)
            m.AriaMemory.objects.filter(student_id=sid).delete()
            m.IntakeSession.objects.filter(Q(student_id=sid) | Q(student_key=sid)).delete()
            profile.conversation_insights = []
            profile.memory_reset_at = now
            profile.save(update_fields=["conversation_insights", "memory_reset_at"])
            expire_rows(m.ChatAttachment.objects.filter(message__in=stale), "file_path")
            stale.delete()
    m.AgentConversationLog.objects.filter(created_at__lt=before(policy.transcript_days)).delete()
    for query in m.PendingQuery.objects.filter(created_at__lt=before(policy.transcript_days)):
        m.UniversityKnowledgeEntry.objects.filter(university_id=query.university_id, topic=query.question, source_type__in=["human_verified", "verified"]).delete()
        m.VerifiedAnswer.objects.filter(query=query).delete()
        query.delete()
    PushToken.objects.filter(updated_at__lt=before(policy.notification_days)).delete()
    NotificationLog.objects.filter(created_at__lt=before(policy.notification_days)).delete()
    m.ChatGeneration.objects.filter(created_at__lt=before(policy.telemetry_days)).exclude(status__in=["queued", "running"]).delete()
    m.ChatModelCall.objects.filter(created_at__lt=before(policy.telemetry_days)).delete()
    m.AgentAuditLog.objects.filter(timestamp__lt=before(policy.telemetry_days)).delete()
    for source in UniversityStudentList.objects.all().iterator():
        override = m.RetentionPolicy.objects.filter(scope=f"institute:{source.institute.uuid}").first()
        days = min(policy.roster_days, override.roster_days) if override else policy.roster_days
        if source.created_at < before(days):
            remove_file(source.source_file_path); source.delete()
    for university in University.objects.all().iterator():
        override = m.RetentionPolicy.objects.filter(scope=f"university:{university.uuid}").first()
        days = min(policy.transcript_days, override.transcript_days) if override else policy.transcript_days
        m.ChatMessage.objects.filter(university_id=str(university.uuid), created_at__lt=before(days)).delete()
        m.UniversityQuestionLog.objects.filter(university_id=str(university.uuid), created_at__lt=before(days)).delete()
        m.PresenterAuditLog.objects.filter(university_id=str(university.uuid), created_at__lt=before(days)).delete()
    m.StudentDeletion.objects.filter(status="completed", completed_at__lt=before(90)).delete()
