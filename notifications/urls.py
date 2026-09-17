from django.urls import path

from notifications import views

urlpatterns = [
    path("register-token/", views.RegisterPushTokenView.as_view(), name="register-push-token"),
    path("unregister-token/", views.UnregisterPushTokenView.as_view(), name="unregister-push-token"),
    path("poll/", views.PollNotificationsView.as_view(), name="poll-notifications"),
]
