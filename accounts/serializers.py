from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from accounts.models import Account
from django_api.models import LinkedInAnalysis, ResumeUpload, StudentProfile


def _is_institute_claim_profile(profile: StudentProfile) -> bool:
    """Return True only for profiles that carry explicit institute-claim provenance."""
    extra = profile.extra_data if isinstance(profile.extra_data, dict) else {}
    return bool(extra.get("claimed_from_institute") or extra.get("institute_sourced"))


def _lock_reusable_claim_profile(email: str) -> StudentProfile | None:
    """
    Find the one profile registration is allowed to adopt.

    Claim-first onboarding creates a StudentProfile before an auth Account
    exists. Registration must attach that exact row instead of minting a
    second profile. We intentionally do NOT adopt arbitrary orphan profiles:
    only an unowned profile with institute-claim provenance is safe to reuse.

    PostgreSQL cannot apply FOR UPDATE to the nullable side of the reverse
    OneToOne outer join produced by `account__isnull=True`, so lock all
    same-email StudentProfile rows first and then remove already-owned rows
    using a separate Account query. Any ambiguity fails closed rather than
    silently creating another identity.
    """
    same_email_profiles = list(
        StudentProfile.objects.select_for_update()
        .filter(email__iexact=email)
        .order_by("created_at", "id")
    )

    if not same_email_profiles:
        return None

    profile_ids = [profile.id for profile in same_email_profiles]
    owned_profile_ids = set(
        Account.objects.filter(student_profile_id__in=profile_ids).values_list("student_profile_id", flat=True)
    )
    candidates = [profile for profile in same_email_profiles if profile.id not in owned_profile_ids]

    if not candidates:
        return None

    claim_profiles = [profile for profile in candidates if _is_institute_claim_profile(profile)]

    if len(claim_profiles) == 1 and len(candidates) == 1:
        return claim_profiles[0]

    if len(claim_profiles) > 1:
        raise serializers.ValidationError(
            {
                "email": (
                    "Multiple institute-claimed profiles exist for this email. "
                    "Registration was stopped to avoid linking the wrong student identity."
                )
            }
        )

    raise serializers.ValidationError(
        {
            "email": (
                "An existing unowned student profile uses this email but cannot be safely linked. "
                "Registration was stopped to avoid creating a duplicate student identity."
            )
        }
    )


class RegisterSerializer(serializers.Serializer):
    """
    Public self-registration -- students only. University accounts are never
    self-registered: a superuser creates the single university-admin account
    via POST /api/superuser/universities/ (see project_superuser.serializers.
    AdminEnrollUniversitySerializer), and the university then enrolls its own
    TOTP device on first login just like every other role.

    Claim-first students are special: /api/claim/confirm/ may already have
    created their StudentProfile before they have a login. In that case this
    serializer reuses the claimed, unowned profile transactionally instead of
    creating a second StudentProfile for the same person.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    role = serializers.ChoiceField(choices=[Account.Role.STUDENT])
    name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")

        # User.email is not unique at the Django model layer. Guard the
        # domain identity as well so a student profile that is already owned
        # by an Account cannot silently acquire a second login identity even
        # if that Account's auth email was changed independently.
        if Account.objects.filter(
            role=Account.Role.STUDENT,
            student_profile__email__iexact=value,
        ).exists():
            raise serializers.ValidationError("A student identity with this email already has an account.")

        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data) -> User:
        email = validated_data["email"]
        name = validated_data.get("name", "")

        with transaction.atomic():
            profile = _lock_reusable_claim_profile(email)

            if profile is None:
                profile = StudentProfile.objects.create(
                    name=name,
                    email=email,
                )
            else:
                # Preserve student-confirmed institute data. Registration only
                # fills blanks and stamps an explicit identity-link marker.
                update_fields: list[str] = []
                if name and not (profile.name or "").strip():
                    profile.name = name
                    update_fields.append("name")
                if profile.email != email:
                    profile.email = email
                    update_fields.append("email")

                extra = dict(profile.extra_data or {})
                if not extra.get("claimed_from_institute"):
                    extra["claimed_from_institute"] = True
                    profile.extra_data = extra
                    update_fields.append("extra_data")

                if update_fields:
                    profile.save(update_fields=update_fields)

            user = User.objects.create_user(
                username=email,
                email=email,
                password=validated_data["password"],
                first_name=name[:150],
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
