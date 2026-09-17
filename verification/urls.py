from django.urls import path

from verification import views

urlpatterns = [
    path("status/", views.VerificationStatusAPIView.as_view(), name="verification-status"),
    path("items/", views.VerificationItemListAPIView.as_view(), name="verification-items"),
]
