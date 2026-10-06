from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction, IntegrityError
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.models import Account
from accounts.permissions import IsUniversityRole, IsTOTPEnrolled
from universities.models import KnowledgeGroup

class StaffInput(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    departments = serializers.ListField(child=serializers.ChoiceField(choices=KnowledgeGroup.Slug.choices), allow_empty=False)

class DepartmentUsersView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def get(self, request):
        rows = Account.objects.filter(role="department", university=request.user.account.university).select_related("user").prefetch_related("departments")
        return Response({"users": [{"id": row.pk, "name": row.user.get_full_name(), "email": row.user.email,
            "departments": list(row.departments.values_list("slug", flat=True)), "active": row.user.is_active} for row in rows]})

    def post(self, request):
        form = StaffInput(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        university = request.user.account.university
        if not university:
            raise serializers.ValidationError("No university is assigned to this account.")
        groups = list(KnowledgeGroup.objects.filter(university=university, slug__in=data["departments"]))
        if len(groups) != len(set(data["departments"])):
            raise serializers.ValidationError({"departments": "Select departments belonging to your university."})
        email = data["email"].strip().lower()
        user = User(username=email, email=email, first_name=data["name"])
        try:
            validate_password(data["password"], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages})
        try:
            with transaction.atomic():
                if User.objects.filter(email__iexact=email).exists():
                    raise serializers.ValidationError({"email": "An account with this email already exists."})
                user.set_password(data["password"])
                user.save()
                account = Account.objects.create(user=user, role="department", university=university)
                account.departments.set(groups)
        except IntegrityError:
            raise serializers.ValidationError({"email": "An account with this email already exists."})
        return Response({"id": account.pk, "name": user.first_name, "email": email}, status=201)
