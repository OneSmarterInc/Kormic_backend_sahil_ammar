from django.urls import path

from django_api.schema_views import DocumentedProfileCreateUpdateAPIView

urlpatterns = [
    path("api/v1/profile/", DocumentedProfileCreateUpdateAPIView.as_view(), name="profile-create-update-contract"),
]

