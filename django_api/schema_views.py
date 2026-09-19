from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import extend_schema, inline_serializer
from drf_spectacular.views import SpectacularAPIView
from rest_framework import serializers
from rest_framework.settings import api_settings

from django_api.serializers import ProfileCreateUpdateSerializer
from django_api.views import ProfileCreateUpdateAPIView

# This module owns the isolated profile OpenAPI contract. Configure the
# inspector before @extend_schema builds its derived schema class so legacy
# DRF AutoSchema views elsewhere in the project do not affect this contract.
api_settings.DEFAULT_SCHEMA_CLASS = AutoSchema


ProfileCreateUpdateResponseSerializer = inline_serializer(
    name="StudentProfileUpdateResponse",
    fields={
        "status": serializers.CharField(),
        "message": serializers.CharField(),
        "student_id": serializers.CharField(),
        "profile_file": serializers.CharField(required=False, allow_blank=True),
        "profile": serializers.DictField(),
    },
)


class DocumentedProfileCreateUpdateAPIView(ProfileCreateUpdateAPIView):
    """Profile endpoint with an explicit OpenAPI request/response contract."""

    schema = AutoSchema()

    @extend_schema(
        operation_id="student_profile_upsert",
        request=ProfileCreateUpdateSerializer,
        responses={200: ProfileCreateUpdateResponseSerializer},
        tags=["student-profile"],
        description=(
            "Create or update the authenticated student's canonical profile. "
            "The request serializer is the source of truth for onboarding/profile field names."
        ),
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class StudentProfileSchemaAPIView(SpectacularAPIView):
    """OpenAPI document intentionally scoped to the student-profile contract."""

    urlconf = "django_api.contract_urls"



from accounts.views import CurrentUserView
from accounts.serializers import PortalUserSerializer


class DocumentedCurrentUserView(CurrentUserView):
    schema = AutoSchema()

    @extend_schema(operation_id="portal_current_user", responses={200: PortalUserSerializer}, tags=["portal-auth"])
    def get(self, request):
        return super().get(request)
