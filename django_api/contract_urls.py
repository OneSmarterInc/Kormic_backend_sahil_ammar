from django.urls import path

from django_api.schema_views import DocumentedProfileCreateUpdateAPIView, DocumentedCurrentUserView

urlpatterns = [
    path("api/v1/auth/me/", DocumentedCurrentUserView.as_view(), name="portal-user-contract"),
    path("api/v1/profile/", DocumentedProfileCreateUpdateAPIView.as_view(), name="profile-create-update-contract"),
]

