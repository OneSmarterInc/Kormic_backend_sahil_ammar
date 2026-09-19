from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from django_api.schema_views import StudentProfileSchemaAPIView
from kormic_backend.app_links import android_assetlinks, apple_app_site_association, claim_landing


def health_check(request):
    """Unauthenticated liveness probe for Docker/compose healthchecks and
    load balancers -- deliberately a plain view, not a DRF one, so it isn't
    subject to DEFAULT_PERMISSION_CLASSES (IsAuthenticated + IsTOTPEnrolled)."""
    return JsonResponse({"status": "ok"})


# Versioned and compatibility routes share handlers, permissions and throttles.
# Keep the old namespace for released clients; new clients use /api/v1/.
api_patterns = [
    path("health/", health_check),
    path("schema/", StudentProfileSchemaAPIView.as_view(), name="openapi-schema"),
    path("auth/", include("accounts.urls")),
    path("verification/", include("verification.urls")),
    path("university-admin/", include("universities.urls")),
    path("notifications/", include("notifications.urls")),
    path("superuser/", include("project_superuser.urls")),
    path("", include("institutes_list.urls")),
    path("", include("django_api.urls")),
]

urlpatterns = [
    path('.well-known/assetlinks.json', android_assetlinks),
    path('.well-known/apple-app-site-association', apple_app_site_association),
    path('claim', claim_landing),
    path('claim/', claim_landing),
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_patterns, "api"), namespace="v1")),
    path("api/", include(api_patterns)),
    path("claim/", include("institutes_list.claim_compat_urls")),
]
