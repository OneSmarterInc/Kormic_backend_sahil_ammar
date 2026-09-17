from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from accounts.models import Account
from django_api.models import StudentProfile

# Same field set universities.views.UniversityProfileAPIView lets a
# university officer patch on their own profile, just reachable here for any
# university_id. Kept as a separate copy (not imported from universities.views)
# since that module's set is underscore-private and this app is meant to
# stay self-contained.
ADMIN_PATCHABLE_UNIVERSITY_FIELDS = {
    "name",
    "location",
    "tagline",
    "description",
    "contact_email",
    "contact_phone",
    "website_url",
    "admissions_office_address",
    "eligibility_criteria",
    "scrape_urls",
    "tone_descriptors",
    "best_fit_notes",
    "not_best_fit_notes",
    "communication_style_notes",
    "never_do_notes",
}

# Subset of the above that's also mirrored into the knowledge base --
# changing any of these should re-sync the derived KB facts.
KB_SYNCED_UNIVERSITY_FIELDS = {
    "description",
    "contact_email",
    "contact_phone",
    "website_url",
    "admissions_office_address",
    "eligibility_criteria",
}


class AdminCreateStudentSerializer(serializers.Serializer):
    """Superuser-driven equivalent of accounts.serializers.RegisterSerializer's
    student branch -- creates the login (User+Account) and a blank
    StudentProfile row in one call, without the student self-registering."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
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
        name = validated_data.get("name", "")

        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=validated_data["password"],
                first_name=name[:150],
            )
            profile = StudentProfile.objects.create(name=name, email=email)
            Account.objects.create(
                user=user, role=Account.Role.STUDENT, student_profile=profile
            )

        return user


class AdminCreateSuperuserSerializer(serializers.Serializer):
    """Lets an existing superuser mint another one, so bootstrapping via the
    create_superuser_account management command only has to happen once per
    deployment."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
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
            Account.objects.create(user=user, role=Account.Role.SUPERUSER)

        return user


class AdminEnrollUniversitySerializer(serializers.Serializer):
    """The only way a university gets onto the platform: a superuser
    registers the University (mirrors universities.services.register_university)
    together with its one admin login, in a single call. There is no
    self-registration path and no separate "officer" concept -- exactly one
    Account (role=university) is created here. TOTP enrollment is
    deliberately NOT done here: the university completes that itself the
    first time it logs in with the email/password the superuser hands it,
    the same must_enroll_totp flow every other role goes through."""

    institution_name = serializers.CharField()
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    name = serializers.CharField(required=False, allow_blank=True, default="")
    profile = serializers.DictField(required=False, default=dict)

    def validate_institution_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("institution_name cannot be blank.")
        return value

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data):
        # Lazy import: mirrors accounts.serializers.RegisterSerializer, which
        # imports universities.models/services inside validate()/create()
        # rather than at module level for the same reason.
        from universities.services import register_university, sync_profile_facts_to_kb

        with transaction.atomic():
            university = register_university(validated_data["institution_name"])

            profile = validated_data.get("profile") or {}
            changed_kb_fields = False
            touched = False
            for field in ADMIN_PATCHABLE_UNIVERSITY_FIELDS:
                if field not in profile:
                    continue
                setattr(university, field, profile[field])
                touched = True
                if field in KB_SYNCED_UNIVERSITY_FIELDS:
                    changed_kb_fields = True

            if touched:
                university.save()
            if changed_kb_fields:
                sync_profile_facts_to_kb(university)

            email = validated_data["email"]
            admin_user = User.objects.create_user(
                username=email,
                email=email,
                password=validated_data["password"],
                first_name=validated_data.get("name", "")[:150],
            )
            Account.objects.create(user=admin_user, role=Account.Role.UNIVERSITY, university=university)

        return university


class AdminEnrollInstituteSerializer(serializers.Serializer):
    """The only way an institute gets onto the platform: a superuser
    registers the Institute (mirrors institutes.services.register_institute)
    together with its one admin login, in a single call -- same shape as
    AdminEnrollUniversitySerializer above. There is no self-registration
    path. TOTP enrollment is NOT done here: the institute completes that
    itself on first login, same as every other role."""

    institution_name = serializers.CharField()
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    name = serializers.CharField(required=False, allow_blank=True, default="")
    contact_email = serializers.CharField(required=False, allow_blank=True, default="")
    contact_phone = serializers.CharField(required=False, allow_blank=True, default="")
    address = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_institution_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("institution_name cannot be blank.")
        return value

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data):
        from institutes.services import register_institute

        with transaction.atomic():
            institute = register_institute(
                validated_data["institution_name"],
                contact_email=validated_data.get("contact_email", ""),
                contact_phone=validated_data.get("contact_phone", ""),
                address=validated_data.get("address", ""),
            )

            email = validated_data["email"]
            admin_user = User.objects.create_user(
                username=email,
                email=email,
                password=validated_data["password"],
                first_name=validated_data.get("name", "")[:150],
            )
            Account.objects.create(user=admin_user, role=Account.Role.INSTITUTE, institute=institute)

        return institute
