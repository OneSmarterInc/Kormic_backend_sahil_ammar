from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import extend_schema, inline_serializer
from drf_spectacular.views import SpectacularAPIView
from rest_framework import serializers

from django_api.serializers import ProfileCreateUpdateSerializer
from django_api.views import ProfileCreateUpdateAPIView


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

    # Keep this endpoint independently inspectable even before the rest of the
    # legacy API is migrated from DRF's default AutoSchema implementation.
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
