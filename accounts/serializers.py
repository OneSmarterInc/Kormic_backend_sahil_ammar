from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from accounts.models import Account
from django_api.models import LinkedInAnalysis, ResumeUpload, StudentProfile


class RegisterSerializer(serializers.Serializer):
    """
    Public self-registration -- students only. University accounts are never
    self-registered: a superuser creates the single university-admin account
    via POST /api/superuser/universities/ (see project_superuser.serializers.
    AdminEnrollUniversitySerializer), and the university then enrolls its own
    TOTP device on first login just like every other role.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    role = serializers.ChoiceField(choices=[Account.Role.STUDENT])
    name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data) -> User:
        email = validated_data["email"]

        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=validated_data["password"],
                first_name=validated_data.get("name", "")[:150],
            )
            profile = StudentProfile.objects.create(
                name=validated_data.get("name", ""),
                email=email,
            )
            Account.objects.create(
                user=user,
                role=Account.Role.STUDENT,
                student_profile=profile,
            )

        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class VerifyTOTPSerializer(serializers.Serializer):
    mfa_token = serializers.CharField()
    code = serializers.CharField()


class EnrollVerifySerializer(serializers.Serializer):
    code = serializers.CharField()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class VerifyResetOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField()

    def validate_otp(self, value: str) -> str:
        code = "".join(str(value or "").split())
        if not code.isdigit() or len(code) != 6:
            raise serializers.ValidationError("Invalid or expired code.")
        return code


class ResetPasswordConfirmSerializer(serializers.Serializer):
    reset_token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value: str) -> str:
        validate_password(value)
        return value


def student_onboarding_status(student_id: str) -> dict:
    """
    Derived (not stored) so it can never drift out of sync with the actual
    data: a student's "already provided this" state is just whatever is in
    the DB right now, not a separately-tracked wizard-completion flag.
    """
    profile = StudentProfile.objects.filter(uuid=student_id).first()
    resume_uploaded = ResumeUpload.objects.filter(student__uuid=student_id).exists()
    github_connected = bool(profile and profile.github)
    # LinkedIn is normally captured via image upload + parsing (not a typed
    # URL), so `profile.linkedin_url` alone stays empty for that path --
    # LinkedInAnalysis rows are the reliable signal. A manually-typed
    # linkedin_url (via the plain profile-update endpoint) also counts.
    linkedin_connected = bool(profile and profile.linkedin_url) or LinkedInAnalysis.objects.filter(
        student__uuid=student_id
    ).exists()

    return {
        "profile_exists": profile is not None,
        "resume_uploaded": resume_uploaded,
        "github_connected": github_connected,
        "linkedin_connected": linkedin_connected,
        "setup_complete": resume_uploaded and github_connected and linkedin_connected,
    }


def serialize_user(user: User) -> dict:
    account = getattr(user, "account", None)
    totp_enrolled = hasattr(user, "totp_device") and user.totp_device.confirmed_at is not None

    data = {
        "id": user.id,
        "email": user.email,
        "name": user.first_name,
        "role": account.role if account else None,
        "student_id": account.student_uuid if account else None,
        "university_id": account.university_uuid if account else None,
        "institute_id": account.institute_uuid if account else None,
        "totp_enrolled": totp_enrolled,
    }

    if account and account.role == Account.Role.STUDENT and account.student_uuid:
        data["onboarding"] = student_onboarding_status(account.student_uuid)

    if account and account.role == Account.Role.UNIVERSITY and account.university_uuid:
        from universities.services import university_setup_status

        data["university_setup_status"] = university_setup_status(account.university_uuid)

    return data
