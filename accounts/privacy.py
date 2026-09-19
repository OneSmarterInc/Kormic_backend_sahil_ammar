"""Authenticated exports and staged, retryable erasure; never serialize credentials."""
import json
import os
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from accounts.permissions import IsStudentRole, IsTOTPEnrolled
from django_api import models as m

class PrivacyThrottle(UserRateThrottle):
    scope = "privacy"
    rate = "5/hour"


def authenticate_action(request):
    password = request.data.get("password")
    if not isinstance(password, str) or not request.user.check_password(password):
        raise PermissionDenied("Confirm your current password to continue.")


def safe_path(value):
    root = Path(settings.MEDIA_ROOT).resolve()
    supplied = Path(value)
    path = (supplied if supplied.is_absolute() else root / supplied).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("UNSAFE_STORED_FILE_PATH")
    return path


def files_for_student(profile):
    paths = [profile.profile_image_path] if profile.profile_image_path else []
    paths += list(profile.resume_uploads.values_list("file_path", flat=True))
    for values in profile.linkedin_analyses.values_list("image_paths", flat=True): paths.extend(values)
    paths += list(m.ChatAttachment.objects.filter(message__student_id=str(profile.uuid)).values_list("file_path", flat=True))
    return set(filter(None, paths))


def rows(queryset):
    result = []
    for row in queryset.values():
        # Files are supplied separately under archive names, never filesystem paths.
        result.append({k: v for k, v in row.items() if k not in {"file_path", "image_paths", "profile_image_path", "otp_hash", "claim_token", "otp_attempts", "otp_expires_at"}})
    return result


class StudentExportView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]
    throttle_classes = [PrivacyThrottle]
    def post(self, request):
        authenticate_action(request)
        profile = request.user.account.student_profile
        if profile is None: raise ValidationError("Create your profile before exporting.")
        sid = str(profile.uuid)
        from verification.models import VerificationCheck, VerificationItem
        from institutes_list.models import ListedStudent
        from accounts.models import GitHubOAuthConnection
        from notifications.models import NotificationLog
        data = {"version": 1, "exported_at": timezone.now(), "account": {"email": request.user.email, "name": request.user.first_name, "onboarding_preferences": request.user.account.onboarding_preferences},
            "profile": rows(m.StudentProfile.objects.filter(pk=profile.pk)),
            "messages": rows(m.ChatMessage.objects.filter(student_id=sid)),
            "attachments": rows(m.ChatAttachment.objects.filter(message__student_id=sid)),
            "resumes": rows(profile.resume_uploads.all()), "linkedin": rows(profile.linkedin_analyses.all()),
            "github": rows(profile.github_analyses.all()), "assessments": rows(profile.fit_assessments.all()),
            "roadmaps": rows(profile.roadmap_versions.all()), "memory": rows(m.AriaMemory.objects.filter(student_id=sid)),
            "intake": rows(m.IntakeSession.objects.filter(Q(student_id=sid) | Q(student_key=sid))),
            "verification": rows(VerificationCheck.objects.filter(student=profile)),
            "verification_items": rows(VerificationItem.objects.filter(verification_check__student=profile)),
            "roster_rows": rows(ListedStudent.objects.filter(claimed_student_id=sid)),
            "notifications": rows(NotificationLog.objects.filter(account=request.user.account)),
            "oauth": list(GitHubOAuthConnection.objects.filter(user=request.user).values("github_username", "scope", "connected_at")),
            "files": []}
        from pure_multi_agent.runtime import _checkpointer
        from langchain_core.messages import message_to_dict
        checkpoint = _checkpointer.get_tuple({"configurable": {"thread_id": sid}})
        messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", []) if checkpoint else []
        data["ai_conversation_memory"] = [message_to_dict(msg) if hasattr(msg, "type") else msg for msg in messages]
        data["escalations"] = rows(m.PendingQuery.objects.filter(student_id=sid))
        data["agent_exchanges"] = rows(m.AgentConversationLog.objects.filter(asker__owner_type="student", asker__owner_id=sid))
        output = tempfile.TemporaryFile()
        try:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                total = 0
                for i, value in enumerate(sorted(files_for_student(profile))):
                    path = safe_path(value)
                    if not path.is_file(): continue
                    total += path.stat().st_size
                    if total > 512 * 1024 * 1024: raise ValidationError("Export exceeds the self-service file limit. Contact support for a full export.")
                    name = f"files/{i}-{path.name}"
                    archive.write(path, name)
                    data["files"].append({"archive_path": name, "filename": path.name})
                archive.writestr("data.json", json.dumps(data, cls=DjangoJSONEncoder))
            output.seek(0)
            response = FileResponse(output, as_attachment=True, filename="kormic-data-export.zip")
            response["Cache-Control"] = "no-store"
            return response
        except BaseException:
            output.close(); raise


def queue_deletion(user):
    with transaction.atomic():
        account = user.account
        # Disable immediately. A grace interval drains in-flight requests/workers.
        user.is_active = False; user.save(update_fields=["is_active"])
        for token in OutstandingToken.objects.filter(user=user): BlacklistedToken.objects.get_or_create(token=token)
        m.ChatGeneration.objects.filter(student_id=account.student_uuid, status="queued").update(status="failed", error_code="ACCOUNT_DELETED", result={})
        job, _ = m.StudentDeletion.objects.get_or_create(user=user, defaults={"student_id": account.student_uuid or "", "not_before": timezone.now() + timedelta(minutes=10)})
    return job


class StudentDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]
    throttle_classes = [PrivacyThrottle]
    def post(self, request):
        authenticate_action(request)
        if request.data.get("confirmation") != "DELETE": raise ValidationError("Type DELETE to confirm account deletion.")
        job = queue_deletion(request.user)
        return Response({"deletion_id": str(job.pk), "status": job.status,
            "message": "Your account is disabled. Erasure will run after active requests finish; external revocation failures are retried."}, status=202)


class DeletionStatusView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [PrivacyThrottle]
    def get(self, request, receipt):
        from django.shortcuts import get_object_or_404
        job = get_object_or_404(m.StudentDeletion, pk=receipt)
        return Response({"status": job.status, "completed_at": job.completed_at})


POLICY_FIELDS = ["upload_days", "transcript_days", "memory_days", "verification_days", "notification_days", "roster_days", "telemetry_days", "inactive_account_days"]
class RetentionPolicyView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled]
    def scope(self, request):
        account = request.user.account
        if account.role == "superuser": return "global"
        if account.role == "university" and account.university_role == "owner": return f"university:{account.university_uuid}"
        if account.role == "institute": return f"institute:{account.institute_uuid}"
        raise PermissionDenied("Only institutional owners or platform administrators can manage retention.")
    def get(self, request):
        scope = self.scope(request)
        defaults, _ = m.RetentionPolicy.objects.get_or_create(scope="global")
        policy = m.RetentionPolicy.objects.filter(scope=scope).first() or defaults
        fields = POLICY_FIELDS if scope == "global" else ["roster_days"] if scope.startswith("institute:") else ["transcript_days"]
        return Response({"scope": scope, "policy": {f: getattr(policy, f) for f in fields}, "defaults": {f: getattr(defaults, f) for f in fields}})
    def patch(self, request):
        scope = self.scope(request)
        fields = POLICY_FIELDS if scope == "global" else ["roster_days"] if scope.startswith("institute:") else ["transcript_days"]
        if not request.data or set(request.data) - set(fields): raise ValidationError("Unsupported retention field for this institution.")
        default, _ = m.RetentionPolicy.objects.get_or_create(scope="global")
        for key, value in request.data.items():
            if type(value) is not int or not 1 <= value <= 3650: raise ValidationError({key: ["Use a whole number of days from 1 to 3650."]})
            if scope != "global" and value > getattr(default, key): raise ValidationError({key: ["An institution can shorten the platform retention period, not extend it."]})
        m.RetentionPolicy.objects.update_or_create(scope=scope, defaults=request.data)
        return self.get(request)
