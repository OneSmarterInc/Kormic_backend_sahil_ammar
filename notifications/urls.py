from django.urls import path

from notifications import views

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification-list"),
    path("unread-count/", views.NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("<int:notification_id>/read/", views.NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("read-all/", views.NotificationMarkAllReadView.as_view(), name="notification-mark-all-read"),
    path("register-token/", views.RegisterPushTokenView.as_view(), name="register-push-token"),
    path("unregister-token/", views.UnregisterPushTokenView.as_view(), name="unregister-push-token"),
    path("poll/", views.PollNotificationsView.as_view(), name="poll-notifications"),
]
