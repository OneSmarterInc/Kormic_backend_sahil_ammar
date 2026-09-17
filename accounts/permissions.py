from __future__ import annotations

from rest_framework.permissions import BasePermission

from accounts.models import Account, TOTPDevice


def get_account(request) -> Account | None:
    try:
        return request.user.account
    except (Account.DoesNotExist, AttributeError):
        return None


class IsTOTPEnrolled(BasePermission):
    """Blocks access until the user has a confirmed TOTP device."""

    message = "TOTP enrollment is required before using this endpoint."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        return TOTPDevice.objects.filter(user=request.user, confirmed_at__isnull=False).exists()


class IsStudentRole(BasePermission):
    message = "This endpoint is only available to student accounts."

    def has_permission(self, request, view) -> bool:
        account = get_account(request)
        return account is not None and account.role == Account.Role.STUDENT


class IsUniversityRole(BasePermission):
    message = "This endpoint is only available to university accounts."

    def has_permission(self, request, view) -> bool:
        account = get_account(request)
        return account is not None and account.role == Account.Role.UNIVERSITY


class IsInstituteRole(BasePermission):
    message = "This endpoint is only available to institute accounts."

    def has_permission(self, request, view) -> bool:
        account = get_account(request)
        return account is not None and account.role == Account.Role.INSTITUTE


class IsStudentOrUniversityRole(BasePermission):
    message = "This endpoint requires a student or university account."

    def has_permission(self, request, view) -> bool:
        account = get_account(request)
        return account is not None and account.role in (Account.Role.STUDENT, Account.Role.UNIVERSITY)


class IsSuperUserRole(BasePermission):
    message = "This endpoint is only available to superuser accounts."

    def has_permission(self, request, view) -> bool:
        account = get_account(request)
        return account is not None and account.role == Account.Role.SUPERUSER


class ScopedToOwnStudentId(BasePermission):
    message = "You may only access your own student profile."

    def has_permission(self, request, view) -> bool:
        student_id = view.kwargs.get("student_id")
        if student_id is None:
            return True
        account = get_account(request)
        return account is not None and account.student_uuid == str(student_id)


class ScopedToOwnUniversityId(BasePermission):
    message = "You may only access your own university's data."

    def has_permission(self, request, view) -> bool:
        university_id = view.kwargs.get("university_id")
        if university_id is None:
            return True
        account = get_account(request)
        return account is not None and account.university_uuid == str(university_id)


class ScopedToOwnInstituteId(BasePermission):
    message = "You may only access your own institute's data."

    def has_permission(self, request, view) -> bool:
        institute_id = view.kwargs.get("institute_id")
        if institute_id is None:
            return True
        account = get_account(request)
        return account is not None and account.institute_uuid == str(institute_id)
