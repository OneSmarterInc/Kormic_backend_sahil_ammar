from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError, PermissionDenied
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from accounts.models import Account
from accounts.permissions import IsTOTPEnrolled, IsUniversityRole, get_account
from universities.models import University, StaffAuditEvent

class IsUniversityOwner(BasePermission):
    def has_permission(self, request, view):
        account = get_account(request)
        return bool(account and account.university_id and account.university_role == "owner")

PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole, IsUniversityOwner]

def serialize_staff(account):
    user = account.user
    return {"id": user.pk, "email": user.email, "name": user.first_name, "role": account.university_role,
            "active": user.is_active, "totp_enrolled": hasattr(user, "totp_device") and user.totp_device.confirmed_at is not None}

def require_current_owner(request, university):
    # Recheck under the university lock: another owner may have revoked the
    # actor while this request waited for a concurrent staff update.
    if not Account.objects.filter(user_id=request.user.pk, university=university,
                                  role=Account.Role.UNIVERSITY, university_role="owner",
                                  user__is_active=True).exists():
        raise PermissionDenied("Only an active university owner can manage staff.")

class CreateStaffSerializer(serializers.Serializer):
    email = serializers.EmailField()
    name = serializers.CharField(max_length=150)
    role = serializers.ChoiceField(choices=Account.UniversityRole.choices)
    initial_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, data):
        data["email"] = data["email"].lower().strip()
        user = User(username=data["email"], email=data["email"], first_name=data["name"])
        try:
            validate_password(data["initial_password"], user)
        except DjangoValidationError as exc:
            raise ValidationError({"initial_password": exc.messages})
        return data

class UpdateStaffSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Account.UniversityRole.choices, required=False)
    active = serializers.BooleanField(required=False)

    def validate(self, data):
        if not data or set(self.initial_data) - set(self.fields):
            raise ValidationError("Supply only role and/or active.")
        return data

class UniversityStaffList(APIView):
    permission_classes = PERMISSIONS

    def get(self, request):
        rows = Account.objects.filter(university=get_account(request).university, role=Account.Role.UNIVERSITY).select_related("user")
        return Response({"staff": [serialize_staff(row) for row in rows], "roles": [{"value": value, "label": label} for value, label in Account.UniversityRole.choices]})

    def post(self, request):
        serializer = CreateStaffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            with transaction.atomic():
                university = University.objects.select_for_update().get(pk=get_account(request).university_id)
                require_current_owner(request, university)
                if User.objects.filter(email__iexact=data["email"]).exists():
                    raise ValidationError({"email": ["An account with this email already exists."]})
                user = User.objects.create_user(username=data["email"], email=data["email"], first_name=data["name"], password=data["initial_password"])
                account = Account.objects.create(user=user, role=Account.Role.UNIVERSITY, university=university, university_role=data["role"])
                StaffAuditEvent.objects.create(university=university, actor=request.user, subject=user, action="created", changes={"role": data["role"]})
        except IntegrityError:
            raise ValidationError({"email": ["An account with this email already exists."]})
        return Response(serialize_staff(account), status=201)

class UniversityStaffDetail(APIView):
    permission_classes = PERMISSIONS

    def patch(self, request, user_id):
        serializer = UpdateStaffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            university = University.objects.select_for_update().get(pk=get_account(request).university_id)
            require_current_owner(request, university)
            account = Account.objects.select_related("user").filter(university=university, user_id=user_id, role=Account.Role.UNIVERSITY).first()
            if account is None:
                raise NotFound("Staff account not found.")
            role = data.get("role", account.university_role)
            active = data.get("active", account.user.is_active)
            if account.university_role == "owner" and account.user.is_active and (role != "owner" or not active):
                owners = Account.objects.filter(university=university, role=Account.Role.UNIVERSITY, university_role="owner", user__is_active=True).count()
                if owners <= 1:
                    raise ValidationError("The university must retain at least one active owner.")
            changes = {"old_role": account.university_role, "role": role, "active": active}
            account.university_role = role
            account.save(update_fields=["university_role", "updated_at"])
            account.user.is_active = active
            account.user.save(update_fields=["is_active"])
            if not active:
                for token in OutstandingToken.objects.filter(user=account.user):
                    BlacklistedToken.objects.get_or_create(token=token)
            StaffAuditEvent.objects.create(university=university, actor=request.user, subject=account.user, action="updated", changes=changes)
        return Response(serialize_staff(account))
