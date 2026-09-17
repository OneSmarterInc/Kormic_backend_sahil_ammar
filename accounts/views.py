from __future__ import annotations

import os

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.db_utils import run_with_retry
from accounts.email import send_otp_email, send_password_changed_email
from accounts.github_oauth import (
    GitHubOAuthError,
    build_authorize_url,
    consume_oauth_state,
    create_oauth_state,
    exchange_code_for_token,
    fetch_github_identity,
    get_connection_for_user,
    revoke_and_delete,
    save_connection,
)
from accounts.mfa import (
    clear_totp_failures,
    create_mfa_session,
    get_user_id_from_mfa_token,
    invalidate_mfa_session,
    is_totp_throttled,
    record_totp_failure,
)
from accounts.models import TOTPBackupCode, TOTPDevice
from accounts.password_reset import (
    OTP_TTL,
    RESET_TOKEN_TTL,
    create_reset_session,
    get_user_id_from_reset_token,
    invalidate_reset_session,
    is_otp_locked,
    is_resend_throttled,
    issue_otp,
    record_otp_failure,
    start_cooldown,
    verify_otp,
)
from accounts.permissions import IsStudentRole, IsTOTPEnrolled
from accounts.serializers import (
    EnrollVerifySerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    RegisterSerializer,
    ResetPasswordConfirmSerializer,
    VerifyResetOTPSerializer,
    VerifyTOTPSerializer,
    serialize_user,
)
from accounts.totp import (
    build_provisioning_uri,
    generate_backup_codes,
    generate_totp_secret,
    hash_backup_code,
    normalize_code,
    verify_totp_code,
)
from verification.services import run_verification


def _github_oauth_html(title: str, message: str) -> str:
    """
    Self-contained confirmation page shown after the GitHub redirect when no
    GITHUB_OAUTH_SUCCESS/FAILURE_REDIRECT_URL is configured for the SPA to
    redirect back into. No external resources, so it's safe under any CSP.
    """
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head>"
        "<body style=\"font-family: sans-serif; text-align: center; padding: 4rem;\">"
        f"<h2>{title}</h2><p>{message}</p>"
        "<p>You can close this tab.</p></body></html>"
    )


def _user_has_confirmed_totp(user: User) -> bool:
    return TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def _student_verification_payload(user: User) -> dict:
    """
    Builds a light-weight verification summary for the logged-in student,
    embedded on login/me responses. Thin wrapper around the single
    canonical check in verification.services.run_verification -- must not
    reimplement the matching logic here, or this and the dedicated
    /api/verification/ endpoints can silently disagree about whether a
    student is "verified".

    Deliberately excludes the per-item detail list (see GET
    /api/verification/items/ for that) so login/me stays small -- this is
    just enough for the frontend to decide whether to route the student to
    the verification screen at all.
    """

    account = getattr(user, "account", None)

    if not account or str(account.role).lower() != "student":
        return {
            "verification_required": False,
            "verification": None,
        }

    student_id = account.student_uuid

    if not student_id:
        return {
            "verification_required": True,
            "verification": {
                "status": "incomplete",
                "verified": False,
                "message": "Student ID is missing for this account.",
                "missing_sources": ["profile", "resume", "github", "linkedin"],
                "pending_items_count": 0,
            },
        }

    verification = run_verification(student_id, user=user)
    verification.pop("items", None)
    return {
        "verification_required": not bool(verification.get("verified")),
        "verification": verification,
    }


def _serialize_user_with_verification(user: User) -> dict:
    user_data = serialize_user(user)
    verification_payload = _student_verification_payload(user)

    user_data["verification_required"] = verification_payload["verification_required"]
    user_data["verification"] = verification_payload["verification"]

    return user_data

class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = run_with_retry(serializer.save)
        log_activity(ActivityLog.Action.REGISTERED, actor=user, target_user=user)

        # Auto-login on register: hand back the same kind of limited,
        # no-refresh access token LoginView issues for a not-yet-enrolled
        # user, so the client can go straight into TOTP enrollment without
        # a separate /login/ call.
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "message": "Account created. Complete TOTP enrollment to finish setup.",
                "must_enroll_totp": True,
                "access": str(refresh.access_token),
                "user": serialize_user(user),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """
    Step 1 of login.

    - No confirmed TOTP device yet: issue a usable access token directly
      (no refresh token) with must_enroll_totp=True. That access token is
      only accepted by the TOTP-gate-exempt endpoints (enroll, verify-
      enrollment, logout, me) until enrollment completes.
    - Confirmed TOTP device: issue an opaque mfa_token instead of any
      tokens; the client must call /verify-totp/ next.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity

        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )

        if user is None or not user.is_active:
            # No account, wrong password, or an existing-but-inactive
            # account -- look the email up regardless so a real account's
            # failed attempts stay linked to it in the audit trail.
            existing = User.objects.filter(email__iexact=serializer.validated_data["email"]).first()
            log_activity(
                ActivityLog.Action.LOGIN_FAILED,
                target_user=existing,
                target_email=serializer.validated_data["email"],
            )
            return Response({"detail": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)

        if _user_has_confirmed_totp(user):
            # Password check passed but MFA is still pending -- the login
            # isn't complete yet, so TOTPLoginVerifyView logs the actual
            # success/failure once the second factor is checked.
            mfa_token = create_mfa_session(user.id)
            return Response(
                {
                    "must_enroll_totp": False,
                    "mfa_token": mfa_token,
                    "totp_required": True,
                    "expires_in": 300,
                },
                status=status.HTTP_200_OK,
            )

        # No TOTP enrolled yet, so there's no second factor to wait on --
        # this token issuance is the whole login.
        log_activity(ActivityLog.Action.LOGIN_SUCCEEDED, actor=user, target_user=user)

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "must_enroll_totp": True,
                "access": str(refresh.access_token),
                "user": serialize_user(user),
            },
            status=status.HTTP_200_OK,
        )


class TOTPEnrollView(APIView):
    
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if _user_has_confirmed_totp(user):
            return Response({"detail": "TOTP is already enrolled."}, status=status.HTTP_400_BAD_REQUEST)

        candidate_secret = generate_totp_secret()
        device, _created = run_with_retry(
            lambda: TOTPDevice.objects.get_or_create(
                user=user, defaults={"secret": candidate_secret, "confirmed_at": None}
            )
        )

        return Response(
            {"secret": device.secret, "provisioning_uri": build_provisioning_uri(device.secret, user.email)},
            status=status.HTTP_200_OK,
        )


class TOTPVerifyEnrollmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = EnrollVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = normalize_code(serializer.validated_data["code"])

        try:
            device = request.user.totp_device
        except TOTPDevice.DoesNotExist:
            return Response({"detail": "TOTP enrollment has not been started."}, status=status.HTTP_400_BAD_REQUEST)

        if device.confirmed_at:
            return Response({"detail": "TOTP is already enrolled."}, status=status.HTTP_400_BAD_REQUEST)

        if not verify_totp_code(device.secret, code):
            return Response({"detail": "Invalid TOTP code."}, status=status.HTTP_400_BAD_REQUEST)

        backup_codes = generate_backup_codes()

        def _confirm():
            with transaction.atomic():
                device.confirmed_at = timezone.now()
                device.save(update_fields=["confirmed_at"])
                TOTPBackupCode.objects.filter(user=request.user).delete()
                TOTPBackupCode.objects.bulk_create(
                    [TOTPBackupCode(user=request.user, code_hash=hash_backup_code(c)) for c in backup_codes]
                )

        run_with_retry(_confirm)

        return Response({"backup_codes": backup_codes}, status=status.HTTP_200_OK)


class TOTPLoginVerifyView(APIView):
    """Step 2 of login: exchange mfa_token + TOTP/backup code for real tokens."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity

        serializer = VerifyTOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mfa_token = serializer.validated_data["mfa_token"].strip()
        code = normalize_code(serializer.validated_data["code"]).upper()

        user_id = get_user_id_from_mfa_token(mfa_token)
        if not user_id:
            return Response(
                {"detail": "Session expired or invalid. Please log in again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if is_totp_throttled(user_id):
            return Response(
                {"detail": "Too many incorrect attempts. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            user = User.objects.get(id=user_id)
            device = user.totp_device
        except (User.DoesNotExist, TOTPDevice.DoesNotExist):
            return Response({"detail": "TOTP is not enrolled."}, status=status.HTTP_400_BAD_REQUEST)

        if not device.confirmed_at:
            return Response({"detail": "TOTP is not enrolled."}, status=status.HTTP_400_BAD_REQUEST)

        is_valid = False
        used_backup_code = None

        if code.isdigit() and len(code) == 6:
            is_valid = verify_totp_code(device.secret, code, user_id=user.id)

        if not is_valid and len(code) == 10:
            code_hash = hash_backup_code(code)
            used_backup_code = TOTPBackupCode.objects.filter(
                user=user, code_hash=code_hash, used_at__isnull=True
            ).first()
            is_valid = used_backup_code is not None

        if not is_valid:
            record_totp_failure(user_id)
            log_activity(ActivityLog.Action.LOGIN_FAILED, target_user=user)
            return Response({"detail": "Invalid TOTP or backup code."}, status=status.HTTP_400_BAD_REQUEST)

        def _mark_used():
            with transaction.atomic():
                if used_backup_code:
                    used_backup_code.used_at = timezone.now()
                    used_backup_code.save(update_fields=["used_at"])
                else:
                    device.last_used_at = timezone.now()
                    device.save(update_fields=["last_used_at"])

        run_with_retry(_mark_used)

        clear_totp_failures(user_id)
        invalidate_mfa_session(mfa_token)
        log_activity(ActivityLog.Action.LOGIN_SUCCEEDED, actor=user, target_user=user)

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": _serialize_user_with_verification(user),
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity

        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"detail": "refresh is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            run_with_retry(RefreshToken(refresh_token).blacklist)
        except Exception:
            return Response({"detail": "Invalid or already-blacklisted refresh token."}, status=status.HTTP_400_BAD_REQUEST)

        log_activity(ActivityLog.Action.LOGGED_OUT, actor=request.user, target_user=request.user)
        return Response(status=status.HTTP_205_RESET_CONTENT)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_serialize_user_with_verification(request.user), status=status.HTTP_200_OK)


_FORGOT_PASSWORD_GENERIC_MESSAGE = (
    "If an account with that email exists, we've sent a one-time code to it."
)
_INVALID_OR_EXPIRED_OTP_MESSAGE = "Invalid or expired code."
_INVALID_OR_EXPIRED_RESET_SESSION_MESSAGE = "This reset session has expired. Please start again."


class ForgotPasswordView(APIView):
    """
    POST /api/auth/forgot-password/  { "email": "..." }

    Step 1 of self-service password reset, open to every role (student,
    university, superuser) since they're all just auth.User + Account --
    see accounts.models.Account. Always answers with the same generic
    message regardless of whether the email matches an account, and always
    starts the resend cooldown keyed on the submitted email itself (not a
    resolved user id), so a caller can't fingerprint real accounts by
    response shape or by how quickly a second request gets throttled.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_reset"

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity

        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()

        if is_resend_throttled(email):
            return Response({"detail": _FORGOT_PASSWORD_GENERIC_MESSAGE}, status=status.HTTP_200_OK)

        start_cooldown(email)

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            otp = issue_otp(user.id)
            send_otp_email(user, otp, ttl_minutes=OTP_TTL // 60)
            log_activity(ActivityLog.Action.PASSWORD_RESET_REQUESTED, target_user=user)

        return Response({"detail": _FORGOT_PASSWORD_GENERIC_MESSAGE}, status=status.HTTP_200_OK)


class VerifyResetOTPView(APIView):
    """
    POST /api/auth/reset-password/verify-otp/  { "email": "...", "otp": "123456" }

    Step 2: proves the caller received the emailed code. On success, hands
    back a short-lived opaque reset_token (mirrors the mfa_token exchange
    in LoginView/TOTPLoginVerifyView) instead of letting this call set the
    password directly -- keeps "proved inbox ownership" and "chose a new
    password" as separate steps.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = VerifyResetOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        otp = serializer.validated_data["otp"]

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return Response({"detail": _INVALID_OR_EXPIRED_OTP_MESSAGE}, status=status.HTTP_400_BAD_REQUEST)

        if is_otp_locked(user.id):
            return Response(
                {"detail": "Too many incorrect attempts. Please request a new code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if not verify_otp(user.id, otp):
            record_otp_failure(user.id)
            return Response({"detail": _INVALID_OR_EXPIRED_OTP_MESSAGE}, status=status.HTTP_400_BAD_REQUEST)

        reset_token = create_reset_session(user.id)
        return Response({"reset_token": reset_token, "expires_in": RESET_TOKEN_TTL}, status=status.HTTP_200_OK)


class ResetPasswordConfirmView(APIView):
    """
    POST /api/auth/reset-password/confirm/  { "reset_token": "...", "new_password": "..." }

    Step 3: spends the one-time reset_token to set a new password, revokes
    every outstanding refresh token for the account (same helper the
    superuser-forced reset uses -- project_superuser.services.
    revoke_all_sessions), and emails a "your password changed" notice so an
    account owner finds out even if the reset wasn't theirs.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "password_reset"

    def post(self, request):
        from project_superuser.models import ActivityLog
        from project_superuser.services import log_activity, revoke_all_sessions

        serializer = ResetPasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reset_token = serializer.validated_data["reset_token"]
        new_password = serializer.validated_data["new_password"]

        user_id = get_user_id_from_reset_token(reset_token)
        if not user_id:
            return Response(
                {"detail": _INVALID_OR_EXPIRED_RESET_SESSION_MESSAGE}, status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            user = User.objects.get(id=user_id, is_active=True)
        except User.DoesNotExist:
            invalidate_reset_session(reset_token)
            return Response(
                {"detail": _INVALID_OR_EXPIRED_RESET_SESSION_MESSAGE}, status=status.HTTP_401_UNAUTHORIZED
            )

        def _reset():
            with transaction.atomic():
                user.set_password(new_password)
                user.save(update_fields=["password"])
                revoke_all_sessions(user)

        run_with_retry(_reset)
        invalidate_reset_session(reset_token)
        log_activity(ActivityLog.Action.PASSWORD_RESET, actor=user, target_user=user)
        send_password_changed_email(user)

        return Response({"detail": "Password has been reset. Please log in again."}, status=status.HTTP_200_OK)


class GitHubOAuthConnectView(APIView):
    """
    GET /api/auth/github/connect/

    Starts the GitHub OAuth flow for the logged-in student. Returns a
    GitHub authorize URL the frontend should do a full browser redirect to
    (not fetch it -- GitHub's login/consent page can't be loaded via XHR).

    A one-time `state` tying this request to the current user is cached for
    a few minutes so /github/callback/ -- which arrives as a plain browser
    redirect from GitHub with no Authorization header -- can tell which
    student to attach the resulting token to, without trusting anything
    GitHub sends other than the code itself.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]
    throttle_scope = "auth"

    def get(self, request):
        try:
            state = create_oauth_state(request.user.id)
            authorize_url = build_authorize_url(state)
        except GitHubOAuthError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"authorize_url": authorize_url}, status=status.HTTP_200_OK)


class GitHubOAuthCallbackView(APIView):
    """
    GET /api/auth/github/callback/

    GitHub redirects the student's browser here after consent. There is no
    JWT on this request, so identity comes solely from the cached `state`
    minted in GitHubOAuthConnectView -- never from anything GitHub passes
    other than the authorization `code`.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def get(self, request):
        error = request.query_params.get("error")
        state = request.query_params.get("state")
        code = request.query_params.get("code")

        if error:
            return self._failure(f"GitHub authorization was not completed ({error}).")

        if not state or not code:
            return self._failure("Missing state or code in GitHub's response.")

        user_id = consume_oauth_state(state)
        if not user_id:
            return self._failure("This connection request expired or was already used. Please try again.")

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return self._failure("Account no longer exists.")

        try:
            token_response = exchange_code_for_token(code)
            identity = fetch_github_identity(token_response["access_token"])
            connection = save_connection(user, token_response, identity)
        except GitHubOAuthError as exc:
            return self._failure(str(exc))

        return self._success(connection.github_username)

    def _success(self, github_username: str):
        redirect_url = os.getenv("GITHUB_OAUTH_SUCCESS_REDIRECT_URL")
        if redirect_url:
            return HttpResponseRedirect(f"{redirect_url}?github=connected&username={github_username}")

        return HttpResponse(
            _github_oauth_html("GitHub connected", f"Connected as @{github_username}."),
            status=status.HTTP_200_OK,
            content_type="text/html",
        )

    def _failure(self, reason: str):
        redirect_url = os.getenv("GITHUB_OAUTH_FAILURE_REDIRECT_URL")
        if redirect_url:
            return HttpResponseRedirect(f"{redirect_url}?github=error")

        return HttpResponse(
            _github_oauth_html("GitHub connection failed", reason),
            status=status.HTTP_400_BAD_REQUEST,
            content_type="text/html",
        )


class GitHubOAuthStatusView(APIView):
    """GET /api/auth/github/status/ -- never exposes the stored token itself."""

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]

    def get(self, request):
        connection = get_connection_for_user(request.user)
        if connection is None:
            return Response({"connected": False}, status=status.HTTP_200_OK)

        return Response(
            {
                "connected": True,
                "github_username": connection.github_username,
                "connected_at": connection.connected_at,
            },
            status=status.HTTP_200_OK,
        )


class GitHubOAuthDisconnectView(APIView):
    """DELETE /api/auth/github/disconnect/"""

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]

    def delete(self, request):
        connection = get_connection_for_user(request.user)
        if connection is None:
            return Response({"detail": "No GitHub connection to remove."}, status=status.HTTP_404_NOT_FOUND)

        revoke_and_delete(connection)
        return Response(status=status.HTTP_204_NO_CONTENT)
