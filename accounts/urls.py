from django.urls import path
from accounts import web_auth

from accounts import views

urlpatterns = [
    path("web/csrf/", web_auth.WebCSRFView.as_view()),
    path("web/login/", web_auth.WebLoginView.as_view()),
    path("web/register/", web_auth.WebRegisterView.as_view()),
    path("web/verify-totp/", web_auth.WebTOTPLoginVerifyView.as_view()),
    path("web/refresh/", web_auth.WebRefreshView.as_view()),
    path("web/logout/", web_auth.WebLogoutView.as_view()),
    path("register/", views.RegisterView.as_view(), name="auth_register"),
    path("login/", views.LoginView.as_view(), name="auth_login"),
    path("verify-totp/", views.TOTPLoginVerifyView.as_view(), name="auth_verify_totp"),
    path("totp/enroll/", views.TOTPEnrollView.as_view(), name="totp_enroll"),
    path("totp/qr/", views.TOTPQRCodeView.as_view(), name="totp_qr"),
    path("totp/verify-enrollment/", views.TOTPVerifyEnrollmentView.as_view(), name="totp_verify_enrollment"),
    path("refresh/", web_auth.NativeTokenRefreshView.as_view(), name="auth_refresh"),
    path("logout/", views.LogoutView.as_view(), name="auth_logout"),
    path("me/", views.CurrentUserView.as_view(), name="auth_me"),
    path("forgot-password/", views.ForgotPasswordView.as_view(), name="auth_forgot_password"),
    path("reset-password/verify-otp/", views.VerifyResetOTPView.as_view(), name="auth_reset_password_verify_otp"),
    path("reset-password/confirm/", views.ResetPasswordConfirmView.as_view(), name="auth_reset_password_confirm"),
    path("github/connect/", views.GitHubOAuthConnectView.as_view(), name="github_oauth_connect"),
    path("github/callback/", views.GitHubOAuthCallbackView.as_view(), name="github_oauth_callback"),
    path("github/status/", views.GitHubOAuthStatusView.as_view(), name="github_oauth_status"),
    path("github/disconnect/", views.GitHubOAuthDisconnectView.as_view(), name="github_oauth_disconnect"),
]
