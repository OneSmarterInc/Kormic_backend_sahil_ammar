from django.urls import path

from project_superuser import views

from django_api.telemetry import ModelTelemetryView

urlpatterns = [
    path("metrics/models/", ModelTelemetryView.as_view()),
    path("students/", views.AdminStudentListCreateAPIView.as_view(), name="superuser-students"),
    path("students/<str:student_id>/", views.AdminStudentDetailAPIView.as_view(), name="superuser-student-detail"),
    path("universities/", views.AdminUniversityListCreateAPIView.as_view(), name="superuser-universities"),
    path(
        "universities/<str:university_id>/",
        views.AdminUniversityDetailAPIView.as_view(),
        name="superuser-university-detail",
    ),
    path("institutes/", views.AdminInstituteListCreateAPIView.as_view(), name="superuser-institutes"),
    path(
        "institutes/<str:institute_id>/",
        views.AdminInstituteDetailAPIView.as_view(),
        name="superuser-institute-detail",
    ),
    path("users/", views.AdminUserListAPIView.as_view(), name="superuser-users"),
    path(
        "users/create-superuser/",
        views.AdminCreateSuperuserAPIView.as_view(),
        name="superuser-create-superuser",
    ),
    path("users/<int:user_id>/", views.AdminUserDetailAPIView.as_view(), name="superuser-user-detail"),
    path(
        "users/<int:user_id>/remove-totp/",
        views.AdminUserRemoveTOTPAPIView.as_view(),
        name="superuser-user-remove-totp",
    ),
    path(
        "users/<int:user_id>/reset-password/",
        views.AdminUserResetPasswordAPIView.as_view(),
        name="superuser-user-reset-password",
    ),
    path(
        "users/<int:user_id>/revoke-sessions/",
        views.AdminUserRevokeSessionsAPIView.as_view(),
        name="superuser-user-revoke-sessions",
    ),
    path("audit-log/", views.ActivityLogListAPIView.as_view(), name="superuser-audit-log"),
    path("agent-audit-logs/", views.AgentAuditLogListAPIView.as_view(), name="superuser-agent-audit-logs"),
    path(
        "metrics/escalations/",
        views.PilotEscalationMetricsAPIView.as_view(),
        name="superuser-metrics-escalations",
    ),
]

