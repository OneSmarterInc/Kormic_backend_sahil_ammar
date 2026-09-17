from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health_check(request):
    """Unauthenticated liveness probe for Docker/compose healthchecks and
    load balancers -- deliberately a plain view, not a DRF one, so it isn't
    subject to DEFAULT_PERMISSION_CLASSES (IsAuthenticated + IsTOTPEnrolled)."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("api/health/", health_check),
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/verification/", include("verification.urls")),
    path("api/university-admin/", include("universities.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api/superuser/", include("project_superuser.urls")),
    path("api/", include("institutes_list.urls")),
    path("api/", include("django_api.urls")),
    # Compatibility for released/local student builds whose API base omitted
    # the /api suffix. Canonical claim URLs remain under /api/claim/.
    path("claim/", include("institutes_list.claim_compat_urls")),
]
