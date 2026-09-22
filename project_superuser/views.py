from __future__ import annotations

from typing import Any, Dict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.db_utils import run_with_retry
from accounts.models import Account, TOTPBackupCode, TOTPDevice
from accounts.permissions import IsSuperUserRole, IsTOTPEnrolled
from accounts.serializers import student_onboarding_status
from django_api.models import StudentProfile
from django_api.services import load_profile_data
from project_superuser import services
from project_superuser.models import ActivityLog
from project_superuser.serializers import (
    ADMIN_PATCHABLE_UNIVERSITY_FIELDS,
    KB_SYNCED_UNIVERSITY_FIELDS,
    AdminCreateStudentSerializer,
    AdminCreateSuperuserSerializer,
    AdminEnrollInstituteSerializer,
    AdminEnrollUniversitySerializer,
)
from institutes.models import Institute
from universities.models import University

SUPERUSER_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]


def _error(message: str, http_status=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"status": "error", "message": str(message)}, status=http_status)


def _serialize_account(account: Account) -> Dict[str, Any]:
    user = account.user
    account_source = "direct"
    source_institute_name = None
    if account.role == Account.Role.STUDENT and account.student_profile_id:
        extra = account.student_profile.extra_data if isinstance(account.student_profile.extra_data, dict) else {}
        institute_source = extra.get("institute_sourced")
        if institute_source or extra.get("claimed_from_institute"):
            account_source = "institute_roster"
            if isinstance(institute_source, dict):
                source_institute_name = institute_source.get("institute_name")

    return {
        "user_id": user.id,
        "email": user.email,
        "name": user.first_name,
        "role": account.role,
        "student_id": account.student_uuid,
        "university_id": account.university_uuid,
        "institute_id": account.institute_uuid,
        "is_active": user.is_active,
        "totp_enrolled": TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).exists(),
        "date_joined": user.date_joined,
        "account_source": account_source,
        "source_institute_name": source_institute_name,
    }


def _serialize_university(university: University) -> Dict[str, Any]:
    from universities.services import university_setup_status

    # Exactly one officer login is created per university today (see
    # AdminEnrollUniversitySerializer) -- surfaced here so a superadmin
    # dashboard can show/contact the login without a separate /users/ call.
    admin_account = (
        Account.objects.filter(university=university, role=Account.Role.UNIVERSITY)
        .select_related("user")
        .order_by("created_at")
        .first()
    )

    return {
        "id": str(university.uuid),
        "name": university.name,
        "country": university.country,
        "agent_name": university.agent_name,
        "location": university.location,
        "tagline": university.tagline,
        "description": university.description,
        "university_email": university.contact_email,
        "contact_email": university.contact_email,
        "contact_phone": university.contact_phone,
        "website_url": university.website_url,
        "admissions_office_address": university.admissions_office_address,
        "eligibility_criteria": university.eligibility_criteria,
        "scrape_urls": university.scrape_urls,
        "tone_descriptors": university.tone_descriptors,
        "best_fit_notes": university.best_fit_notes,
        "not_best_fit_notes": university.not_best_fit_notes,
        "communication_style_notes": university.communication_style_notes,
        "never_do_notes": university.never_do_notes,
        "admin_user_id": admin_account.user_id if admin_account else None,
        "admin_email": admin_account.user.email if admin_account else None,
        "admin_name": admin_account.user.first_name if admin_account else None,
        "admin_is_active": admin_account.user.is_active if admin_account else None,
        "admin_totp_enrolled": (
            TOTPDevice.objects.filter(user_id=admin_account.user_id, confirmed_at__isnull=False).exists()
            if admin_account
            else None
        ),
        "officer_count": Account.objects.filter(university=university, role=Account.Role.UNIVERSITY).count(),
        "setup_status": university_setup_status(str(university.uuid)),
        "created_at": university.created_at,
        "updated_at": university.updated_at,
    }


# ---------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------

class AdminStudentListCreateAPIView(APIView):
    """
    GET /api/superuser/students/   ?search=<email substring>
    POST /api/superuser/students/  Body: {"email", "password", "name"}
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        accounts = Account.objects.filter(role=Account.Role.STUDENT).select_related("user").order_by("-created_at")

        search = request.query_params.get("search", "").strip()
        if search:
            accounts = accounts.filter(user__email__icontains=search)

        profile_pks = [a.student_profile_id for a in accounts if a.student_profile_id]
        profiles_by_id = {
            str(p.uuid): p
            for p in StudentProfile.objects.filter(pk__in=profile_pks)
        }

        students = []
        for account in accounts:
            profile = profiles_by_id.get(account.student_uuid)
            students.append({
                **_serialize_account(account),
                "institution": profile.institution if profile else "",
                "major": profile.major if profile else "",
                "verified": profile.verified if profile else False,
            })

        return Response({"students": students})

    def post(self, request):
        serializer = AdminCreateStudentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = run_with_retry(serializer.save)
        return Response(_serialize_account(user.account), status=status.HTTP_201_CREATED)


class AdminStudentDetailAPIView(APIView):
    """
    GET /api/superuser/students/<student_id>/
    DELETE /api/superuser/students/<student_id>/
        Removes the login (User, cascading Account/TOTP/GitHub OAuth) and
        purges the StudentProfile and everything else keyed by student_id
        (see project_superuser.services.purge_student_data).
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def _get_account(self, student_id: str):
        return (
            Account.objects.filter(role=Account.Role.STUDENT, student_profile__uuid=student_id)
            .select_related("user")
            .first()
        )

    def get(self, request, student_id: str):
        account = self._get_account(student_id)
        if account is None:
            return _error("Student not found.", status.HTTP_404_NOT_FOUND)

        data = _serialize_account(account)
        data["profile"] = load_profile_data(student_id)
        data["onboarding"] = student_onboarding_status(student_id)
        return Response(data)

    def delete(self, request, student_id: str):
        account = self._get_account(student_id)
        if account is None:
            return _error("Student not found.", status.HTTP_404_NOT_FOUND)

        def _do_delete():
            with transaction.atomic():
                account.user.delete()
                services.purge_student_data(student_id)

        run_with_retry(_do_delete)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------
# Universities
# ---------------------------------------------------------------------

class AdminUniversityListCreateAPIView(APIView):
    """
    GET /api/superuser/universities/   ?search=<name substring>
    POST /api/superuser/universities/  Body:
        {
          "institution_name": "...",
          "email": "...", "password": "...", "name": "...",
          "profile": {...any of ADMIN_PATCHABLE_UNIVERSITY_FIELDS...}
        }
    Creates the University plus its one admin login in a single call --
    email/password/institution_name are required; there is no bare-university
    (no-login) or multi-officer path. `name` is the admin's own name; `profile`
    carries basic info (location, contacts, description, etc). The university
    enrolls its own TOTP device on first login, same as every other role.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        universities = University.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            universities = universities.filter(name__icontains=search)

        return Response({"universities": [_serialize_university(u) for u in universities]})

    def post(self, request):
        serializer = AdminEnrollUniversitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        university = run_with_retry(serializer.save)
        return Response(_serialize_university(university), status=status.HTTP_201_CREATED)


class AdminUniversityDetailAPIView(APIView):
    """
    GET /api/superuser/universities/<university_id>/
    PATCH /api/superuser/universities/<university_id>/  Body: any of ADMIN_PATCHABLE_UNIVERSITY_FIELDS
    DELETE /api/superuser/universities/<university_id>/
        Refuses (409) while officer accounts still reference this
        university_id -- remove/reassign them via /api/superuser/users/
        first, so no login is left pointing at a deleted university.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request, university_id: str):
        university = University.objects.filter(uuid=university_id).first()
        if university is None:
            return _error("University not found.", status.HTTP_404_NOT_FOUND)
        return Response(_serialize_university(university))

    def patch(self, request, university_id: str):
        university = University.objects.filter(uuid=university_id).first()
        if university is None:
            return _error("University not found.", status.HTTP_404_NOT_FOUND)

        data = request.data or {}
        changed_kb_fields = False
        touched = False
        for field in ADMIN_PATCHABLE_UNIVERSITY_FIELDS:
            if field not in data:
                continue
            setattr(university, field, data[field])
            touched = True
            if field in KB_SYNCED_UNIVERSITY_FIELDS:
                changed_kb_fields = True

        if not touched:
            return _error(f"Provide at least one of: {', '.join(sorted(ADMIN_PATCHABLE_UNIVERSITY_FIELDS))}.")

        university.save()

        if changed_kb_fields:
            from universities.services import sync_profile_facts_to_kb

            sync_profile_facts_to_kb(university)

        return Response(_serialize_university(university))

    def delete(self, request, university_id: str):
        university = University.objects.filter(uuid=university_id).first()
        if university is None:
            return _error("University not found.", status.HTTP_404_NOT_FOUND)

        if Account.objects.filter(university__uuid=university_id, role=Account.Role.UNIVERSITY).exists():
            return _error(
                "This university still has officer accounts. Remove or reassign them via "
                "/api/superuser/users/ first.",
                status.HTTP_409_CONFLICT,
            )

        def _do_delete():
            with transaction.atomic():
                university.delete()
                services.purge_university_data(university_id)

        run_with_retry(_do_delete)
        return Response(status=status.HTTP_204_NO_CONTENT)


# Institutes
# ---------------------------------------------------------------------

def _serialize_institute(institute: Institute) -> Dict[str, Any]:
    # Exactly one admin login is created per institute today same pattern as _serialize_university.
    admin_account = (
        Account.objects.filter(institute=institute, role=Account.Role.INSTITUTE)
        .select_related("user")
        .order_by("created_at")
        .first()
    )

    return {
        "id": str(institute.uuid),
        "name": institute.name,
        "country": institute.country,
        "contact_email": institute.contact_email,
        "contact_phone": institute.contact_phone,
        "address": institute.address,
        "admin_user_id": admin_account.user_id if admin_account else None,
        "admin_email": admin_account.user.email if admin_account else None,
        "admin_name": admin_account.user.first_name if admin_account else None,
        "admin_is_active": admin_account.user.is_active if admin_account else None,
        "admin_totp_enrolled": (
            TOTPDevice.objects.filter(user_id=admin_account.user_id, confirmed_at__isnull=False).exists()
            if admin_account
            else None
        ),
        "created_at": institute.created_at,
        "updated_at": institute.updated_at,
    }


class AdminInstituteListCreateAPIView(APIView):
    """
    GET /api/superuser/institutes/   ?search=<name substring>
    POST /api/superuser/institutes/  Body:
        {
          "institution_name": "...",
          "email": "...", "password": "...", "name": "...",
          "contact_email": "...", "contact_phone": "...", "address": "..."
        }
    Creates the Institute plus its one admin login in a single call, same
    pattern as AdminUniversityListCreateAPIView -- institutes upload student
    lists for the claim flow (institutes_list) but never get an AI agent.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        institutes = Institute.objects.all()

        search = request.query_params.get("search", "").strip()
        if search:
            institutes = institutes.filter(name__icontains=search)

        return Response({"institutes": [_serialize_institute(i) for i in institutes]})

    def post(self, request):
        serializer = AdminEnrollInstituteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        institute = run_with_retry(serializer.save)
        return Response(_serialize_institute(institute), status=status.HTTP_201_CREATED)


class AdminInstituteDetailAPIView(APIView):
    """
    GET /api/superuser/institutes/<institute_id>/
    PATCH /api/superuser/institutes/<institute_id>/  Body: name/contact_email/contact_phone/address
    DELETE /api/superuser/institutes/<institute_id>/
        Refuses (409) while admin accounts or uploaded lists still reference
        this institute_id -- remove/reassign the admin via /api/superuser/users/
        and the lists stay owned by the institute (InstituteStudentList.institute
        is on_delete=PROTECT) until reassigned or removed first.
    """

    permission_classes = SUPERUSER_PERMISSIONS
    PATCHABLE_FIELDS = {"name", "contact_email", "contact_phone", "address"}

    def get(self, request, institute_id: str):
        institute = Institute.objects.filter(uuid=institute_id).first()
        if institute is None:
            return _error("Institute not found.", status.HTTP_404_NOT_FOUND)
        return Response(_serialize_institute(institute))

    def patch(self, request, institute_id: str):
        institute = Institute.objects.filter(uuid=institute_id).first()
        if institute is None:
            return _error("Institute not found.", status.HTTP_404_NOT_FOUND)

        data = request.data or {}
        touched = False
        for field in self.PATCHABLE_FIELDS:
            if field not in data:
                continue
            setattr(institute, field, data[field])
            touched = True

        if not touched:
            return _error(f"Provide at least one of: {', '.join(sorted(self.PATCHABLE_FIELDS))}.")

        institute.save()
        return Response(_serialize_institute(institute))

    def delete(self, request, institute_id: str):
        institute = Institute.objects.filter(uuid=institute_id).first()
        if institute is None:
            return _error("Institute not found.", status.HTTP_404_NOT_FOUND)

        if Account.objects.filter(institute__uuid=institute_id, role=Account.Role.INSTITUTE).exists():
            return _error(
                "This institute still has an admin account. Remove or reassign it via "
                "/api/superuser/users/ first.",
                status.HTTP_409_CONFLICT,
            )

        from institutes_list.models import InstituteStudentList

        if InstituteStudentList.objects.filter(institute__uuid=institute_id).exists():
            return _error(
                "This institute still has uploaded student lists and cannot be deleted.",
                status.HTTP_409_CONFLICT,
            )

        institute.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------
# Roster students (all institute-uploaded database rows)
# ---------------------------------------------------------------------

class AdminRosterStudentListAPIView(APIView):
    """
    GET /api/superuser/roster-students/
        ?search=<name/email/program>
        &status=unclaimed|claimed|revoked|expired
        &institute_id=<uuid>
        &account_state=with_account|without_account
        &page=<n>&page_size=<1-100>

    Every accepted institute roster row is visible here immediately, before
    or after claim/registration. Login-account state is annotated in SQL so
    filtering and pagination remain bounded for large datasets.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        from django.db.models import Count, Exists, OuterRef, Q
        from institutes_list.models import ListedStudent

        student_account_for_email = Account.objects.filter(
            role=Account.Role.STUDENT,
            user__email__iexact=OuterRef("email"),
        )

        qs = (
            ListedStudent.objects
            .select_related("source_list__institute")
            .annotate(has_account=Exists(student_account_for_email))
            .order_by("-created_at", "-id")
        )

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(full_name__icontains=search)
                | Q(email__icontains=search)
                | Q(field_of_study__icontains=search)
                | Q(program_name__icontains=search)
                | Q(source_list__institute__name__icontains=search)
            )

        row_status = request.query_params.get("status", "").strip().lower()
        if row_status:
            if row_status not in ListedStudent.Status.values:
                return _error("Invalid roster status.")
            qs = qs.filter(status=row_status)

        institute_id = request.query_params.get("institute_id", "").strip()
        if institute_id:
            qs = qs.filter(source_list__institute__uuid=institute_id)

        account_state = request.query_params.get("account_state", "").strip().lower()
        if account_state not in {"", "with_account", "without_account"}:
            return _error("account_state must be with_account or without_account.")
        if account_state == "with_account":
            qs = qs.filter(has_account=True)
        elif account_state == "without_account":
            qs = qs.filter(has_account=False)

        try:
            page = max(1, int(request.query_params.get("page", "1")))
            page_size = min(100, max(1, int(request.query_params.get("page_size", "25"))))
        except (TypeError, ValueError):
            return _error("page and page_size must be positive integers.")

        total = qs.count()
        start = (page - 1) * page_size
        page_rows = list(qs[start : start + page_size])

        emails = [row.email for row in page_rows if row.has_account and row.email]
        claimed_ids = [row.claimed_student_id for row in page_rows if row.claimed_student_id]

        account_filter = Q(student_profile__uuid__in=claimed_ids)
        for email in emails:
            account_filter |= Q(user__email__iexact=email)

        page_accounts = (
            Account.objects
            .filter(role=Account.Role.STUDENT)
            .filter(account_filter)
            .select_related("user", "student_profile")
        ) if (emails or claimed_ids) else Account.objects.none()

        accounts_by_student_uuid = {
            str(account.student_profile.uuid): account
            for account in page_accounts
            if account.student_profile_id
        }
        accounts_by_email = {
            (account.user.email or "").lower(): account
            for account in page_accounts
            if account.user.email
        }

        results = []
        for row in page_rows:
            account = (
                accounts_by_student_uuid.get(row.claimed_student_id)
                if row.claimed_student_id
                else None
            ) or accounts_by_email.get((row.email or "").lower())
            institute = row.source_list.institute
            results.append({
                "id": row.id,
                "list_id": row.source_list_id,
                "institute_id": str(institute.uuid),
                "institute_name": institute.name,
                "full_name": row.full_name,
                "email": row.email,
                "field_of_study": row.field_of_study,
                "degree_level": row.degree_level,
                "program_name": row.program_name,
                "expected_graduation": row.expected_graduation,
                "country": row.country,
                "status": row.status,
                "invited_at": row.invited_at,
                "claimed_at": row.claimed_at,
                "claimed_student_id": row.claimed_student_id or None,
                "has_account": bool(row.has_account),
                "account_user_id": account.user_id if account else None,
                "account_email": account.user.email if account else None,
                "account_active": account.user.is_active if account else None,
                "created_at": row.created_at,
            })

        base_rows = ListedStudent.objects.annotate(has_account=Exists(student_account_for_email))
        status_counts = {
            item["status"]: item["count"]
            for item in base_rows.values("status").annotate(count=Count("id"))
        }

        return Response({
            "results": results,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": start + page_size < total,
            },
            "summary": {
                "total": base_rows.count(),
                "unclaimed": status_counts.get(ListedStudent.Status.UNCLAIMED, 0),
                "claimed": status_counts.get(ListedStudent.Status.CLAIMED, 0),
                "with_account": base_rows.filter(has_account=True).count(),
            },
        })


# ---------------------------------------------------------------------
# Users (cross-role)
# ---------------------------------------------------------------------

class AdminUserListAPIView(APIView):
    """
    GET /api/superuser/users/   ?role=student|university|institute|superuser   &search=<email substring>
    Every login account across all roles -- the cross-role view /students/
    and /universities/ don't give you.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        accounts = Account.objects.select_related("user", "student_profile").order_by("-created_at")

        role = request.query_params.get("role", "").strip()
        if role:
            accounts = accounts.filter(role=role)

        search = request.query_params.get("search", "").strip()
        if search:
            accounts = accounts.filter(user__email__icontains=search)

        return Response({"users": [_serialize_account(a) for a in accounts]})


class AdminCreateSuperuserAPIView(APIView):
    """POST /api/superuser/users/create-superuser/  Body: {"email", "password", "name"}"""

    permission_classes = SUPERUSER_PERMISSIONS

    def post(self, request):
        serializer = AdminCreateSuperuserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = run_with_retry(serializer.save)
        return Response(_serialize_account(user.account), status=status.HTTP_201_CREATED)


class AdminUserDetailAPIView(APIView):
    """
    GET /api/superuser/users/<user_id>/
    PATCH /api/superuser/users/<user_id>/  Body: {"is_active": true|false}
    DELETE /api/superuser/users/<user_id>/
        Removes only this login (User, cascading Account/TOTP/GitHub OAuth).
        Does not touch the underlying StudentProfile/University row -- use
        /api/superuser/students/<id>/ or /api/superuser/universities/<id>/
        for a full data purge.
    A superuser may not deactivate or delete their own account through this
    endpoint, to avoid locking every superuser out at once.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def _get_account(self, user_id: int):
        return Account.objects.filter(user_id=user_id).select_related("user").first()

    def get(self, request, user_id: int):
        account = self._get_account(user_id)
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)
        return Response(_serialize_account(account))

    def patch(self, request, user_id: int):
        account = self._get_account(user_id)
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)

        if "is_active" not in (request.data or {}):
            return _error("is_active is required.")

        if account.user_id == request.user.id:
            return _error("You cannot change your own active status.")

        account.user.is_active = bool(request.data["is_active"])
        account.user.save(update_fields=["is_active"])
        return Response(_serialize_account(account))

    def delete(self, request, user_id: int):
        account = self._get_account(user_id)
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)

        if account.user_id == request.user.id:
            return _error("You cannot delete your own account.")

        run_with_retry(account.user.delete)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminUserRemoveTOTPAPIView(APIView):
    """
    POST /api/superuser/users/<user_id>/remove-totp/
    Deletes the user's TOTPDevice and any unused backup codes, so they
    re-enter the must_enroll_totp flow on their next login. For when
    someone's lost their authenticator and has no backup codes left --
    there is no self-service equivalent, so this is the only recovery
    path. A superuser may not remove their own TOTP through this endpoint
    (same self-protection as the is_active/delete actions above).
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def post(self, request, user_id: int):
        account = Account.objects.filter(user_id=user_id).select_related("user").first()
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)

        if account.user_id == request.user.id:
            return _error("You cannot remove your own TOTP through this endpoint.")

        def _do_remove():
            with transaction.atomic():
                TOTPDevice.objects.filter(user_id=user_id).delete()
                TOTPBackupCode.objects.filter(user_id=user_id).delete()
                services.log_activity(ActivityLog.Action.TOTP_REMOVED, actor=request.user, target_user=account.user)

        run_with_retry(_do_remove)
        return Response(_serialize_account(account))


class AdminUserResetPasswordAPIView(APIView):
    """
    POST /api/superuser/users/<user_id>/reset-password/  Body: {"password": "..."}
    Sets a new password directly and revokes every outstanding refresh
    token for the user, so old sessions can't keep running under the
    password they no longer know.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def post(self, request, user_id: int):
        account = Account.objects.filter(user_id=user_id).select_related("user").first()
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)

        password = (request.data or {}).get("password")
        if not password:
            return _error("password is required.")

        try:
            validate_password(password, user=account.user)
        except DjangoValidationError as exc:
            return _error(" ".join(exc.messages))

        def _do_reset():
            with transaction.atomic():
                account.user.set_password(password)
                account.user.save(update_fields=["password"])
                services.revoke_all_sessions(account.user)
                services.log_activity(ActivityLog.Action.PASSWORD_RESET, actor=request.user, target_user=account.user)

        run_with_retry(_do_reset)
        return Response(_serialize_account(account))


class AdminUserRevokeSessionsAPIView(APIView):
    """
    POST /api/superuser/users/<user_id>/revoke-sessions/
    Blacklists every outstanding refresh token for the user (force logout
    everywhere). Any access token they're currently holding still works
    until it naturally expires (ACCESS_TOKEN_LIFETIME, 30 min) -- this
    only stops them getting a new one without logging in again.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def post(self, request, user_id: int):
        account = Account.objects.filter(user_id=user_id).select_related("user").first()
        if account is None:
            return _error("User not found.", status.HTTP_404_NOT_FOUND)

        def _do_revoke():
            with transaction.atomic():
                revoked = services.revoke_all_sessions(account.user)
                services.log_activity(ActivityLog.Action.SESSIONS_REVOKED, actor=request.user, target_user=account.user)
                return revoked

        revoked_count = run_with_retry(_do_revoke)
        return Response({**_serialize_account(account), "revoked_sessions": revoked_count})


# ---------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------

class ActivityLogListAPIView(APIView):
    """
    GET /api/superuser/audit-log/
        ?user_id=<id>
        &action=totp_removed|password_reset|sessions_revoked|registered|login_succeeded|login_failed|logged_out
        &role=student|university|superuser
        &university_id=<id>
        &student_id=<id>
        &limit=<n>
    Read-only trail covering both admin-initiated actions (TOTP removal,
    password reset, session revocation) and self-service auth activity
    (register/login/logout, logged from accounts.views on this same
    model), newest first.
    - `user_id` matches either actor or target.
    - `action` filters to one action type.
    - `role` filters by the target's role at the time of the event.
    - `university_id`/`student_id` filter to one specific university's or
      student's activity (e.g. one university's full login history).
    - `limit` defaults to 100, capped at 500.
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        from django.db.models import Q

        entries = ActivityLog.objects.all()

        user_id = request.query_params.get("user_id", "").strip()
        if user_id:
            entries = entries.filter(Q(actor_id=user_id) | Q(target_user_id=user_id))

        action = request.query_params.get("action", "").strip()
        if action:
            entries = entries.filter(action=action)

        role = request.query_params.get("role", "").strip()
        if role:
            entries = entries.filter(target_role=role)

        university_id = request.query_params.get("university_id", "").strip()
        if university_id:
            entries = entries.filter(target_university_id=university_id)

        student_id = request.query_params.get("student_id", "").strip()
        if student_id:
            entries = entries.filter(target_student_id=student_id)

        try:
            limit = min(int(request.query_params.get("limit", 100)), 500)
        except ValueError:
            limit = 100

        return Response({
            "entries": [
                {
                    "id": entry.id,
                    "actor_email": entry.actor_email,
                    "action": entry.action,
                    "target_email": entry.target_email,
                    "target_role": entry.target_role,
                    "target_student_id": entry.target_student_id,
                    "target_university_id": entry.target_university_id,
                    "created_at": entry.created_at,
                }
                for entry in entries[:limit]
            ]
        })


class AgentAuditLogListAPIView(APIView):
    """
    GET /api/superuser/agent-audit-logs/
        ?since_id=<id> (for live polling)
        &student_id=<id> (filter by student)
        &limit=<n> (default 50, max 200)
    
    Provides a real-time (short-polling) stream of agent telemetry, including
    reasoning steps, tool calls, and agent-to-agent communication.
    """
    
    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        from django_api.models import AgentAuditLog
        
        entries = AgentAuditLog.objects.all()
        
        since_id = request.query_params.get("since_id", "").strip()
        if since_id.isdigit():
            entries = entries.filter(id__gt=int(since_id))
            
        student_id = request.query_params.get("student_id", "").strip()
        if student_id:
            entries = entries.filter(student_id=student_id)
            
        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except ValueError:
            limit = 50
            
        # Ensure we return the oldest-to-newest for live polling appending if since_id is provided,
        # otherwise return newest-first.
        if since_id:
            entries = entries.order_by("timestamp")[:limit]
            ordered_entries = list(entries)
        else:
            entries = entries.order_by("-timestamp")[:limit]
            ordered_entries = list(entries)[::-1] # Reverse to display oldest to newest in the chunk
            
        return Response({
            "logs": [
                {
                    "id": log.id,
                    "run_id": log.run_id,
                    "student_id": log.student_id,
                    "actor_agent": log.actor_agent,
                    "action_type": log.action_type,
                    "target": log.target,
                    "inputs": log.inputs,
                    "outputs": log.outputs,
                    "timestamp": log.timestamp,
                }
                for log in ordered_entries
            ]
        })



class PilotEscalationMetricsAPIView(APIView):
    """
    GET /api/superuser/metrics/escalations/
        ?university_id=<id>   (optional -- omit to combine every university)
        &weeks=<n>             (optional, default 12, max 52)
    A4: the number the Fall pilot is measured by -- escalations per student
    per week, broken down by knowledge group, over time. Flat/zero weeks
    are returned as-is (real numbers, pre-pilot flat is expected and fine).
    """

    permission_classes = SUPERUSER_PERMISSIONS

    def get(self, request):
        university_id = request.query_params.get("university_id", "").strip()

        try:
            weeks = int(request.query_params.get("weeks", 12))
        except ValueError:
            return _error("weeks must be an integer.")

        return Response(services.escalation_metrics(university_id=university_id, weeks=weeks))
