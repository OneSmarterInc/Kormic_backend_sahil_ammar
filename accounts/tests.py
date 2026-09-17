from unittest import mock

import pyotp
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice

STUDENT_EMAIL = "student1@example.com"


def sid(email=STUDENT_EMAIL):
    """The student_profile uuid for a registered account, as a string."""
    return str(Account.objects.get(user__email=email).student_profile.uuid)


def register(client, **overrides):
    payload = {
        "email": STUDENT_EMAIL,
        "password": "S3curePassw0rd!",
        "role": "student",
        "name": "Student One",
    }
    payload.update(overrides)
    return client.post("/api/auth/register/", payload, format="json")


def login(client, email=STUDENT_EMAIL, password="S3curePassw0rd!"):
    return client.post("/api/auth/login/", {"email": email, "password": password}, format="json")


def enroll_and_confirm(client, access_token):
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    enroll_resp = client.post("/api/auth/totp/enroll/")
    secret = enroll_resp.data["secret"]
    code = pyotp.TOTP(secret).now()
    verify_resp = client.post("/api/auth/totp/verify-enrollment/", {"code": code}, format="json")
    return enroll_resp, verify_resp, secret


class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()

    def test_register_student_success(self):
        resp = register(self.client)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Account.objects.get(user__email=STUDENT_EMAIL).role, "student")

    def test_register_student_ignores_client_supplied_student_id(self):
        resp = register(self.client, student_id="someone-elses-id")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        # The server mints its own uuid; a client-supplied value is ignored.
        self.assertEqual(resp.data["user"]["student_id"], sid())
        self.assertNotEqual(resp.data["user"]["student_id"], "someone-elses-id")

    def test_register_endpoint_rejects_university_role(self):
        # Universities never self-register -- only a superuser can create one,
        # via POST /api/superuser/universities/.
        resp = self.client.post(
            "/api/auth/register/",
            {"email": "sneaky_officer@wsu.edu", "password": "S3curePassw0rd!", "role": "university"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Account.objects.filter(role="university").exists())

    def test_register_duplicate_email_rejected(self):
        register(self.client)
        resp = register(self.client)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_similar_emails_get_distinct_student_ids(self):
        # Identifiers are random uuids now -- no email-derived slug collision.
        r1 = register(self.client, email="student.one@example.com")
        r2 = register(self.client, email="student+one@example.com")
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(r1.data["user"]["student_id"], r2.data["user"]["student_id"])

    def test_login_unenrolled_user_gets_restricted_token(self):
        register(self.client)
        resp = login(self.client)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["must_enroll_totp"])
        self.assertIn("access", resp.data)
        self.assertNotIn("refresh", resp.data)

    def test_restricted_token_blocked_from_protected_endpoint(self):
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.get(f"/api/profile/{sid()}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_restricted_token_allows_enroll_and_verify_enrollment(self):
        register(self.client)
        access = login(self.client).data["access"]
        enroll_resp, verify_resp, _ = enroll_and_confirm(self.client, access)
        self.assertEqual(enroll_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(verify_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(verify_resp.data["backup_codes"]), 10)
        device = TOTPDevice.objects.get(user__email=STUDENT_EMAIL)
        self.assertIsNotNone(device.confirmed_at)

    def test_double_enroll_call_returns_same_secret(self):
        """
        Regression test: TOTPEnrollView used to regenerate the secret on
        every call, so a second call (a double-submitted form, a retried
        request, a re-rendered enrollment screen) would silently invalidate
        the QR the authenticator app already scanned from the first call --
        every code it produced afterward would fail verification no matter
        what the student entered. Calling enroll twice must be a no-op.
        """
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        first = self.client.post("/api/auth/totp/enroll/")
        second = self.client.post("/api/auth/totp/enroll/")
        self.assertEqual(first.data["secret"], second.data["secret"])
        self.assertEqual(TOTPDevice.objects.filter(user__email=STUDENT_EMAIL).count(), 1)

        # A code generated from the FIRST response's secret (the one the
        # student's authenticator app actually scanned) must still verify.
        code = pyotp.TOTP(first.data["secret"]).now()
        verify_resp = self.client.post("/api/auth/totp/verify-enrollment/", {"code": code}, format="json")
        self.assertEqual(verify_resp.status_code, status.HTTP_200_OK)

    def test_verify_enrollment_tolerates_code_with_embedded_space(self):
        """
        Regression test: several authenticator apps display the 6-digit
        code with a space in the middle ("123 456") for readability, and a
        copy-paste can carry that space into the request. A bare .strip()
        only removes leading/trailing whitespace, so code.isdigit() would
        silently fail on the internal space and report "Invalid TOTP code"
        for a code that was actually correct.
        """
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        secret = self.client.post("/api/auth/totp/enroll/").data["secret"]

        raw_code = pyotp.TOTP(secret).now()
        spaced_code = f"{raw_code[:3]} {raw_code[3:]}"
        resp = self.client.post("/api/auth/totp/verify-enrollment/", {"code": spaced_code}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_same_access_token_now_passes_totp_gate_after_enrollment(self):
        register(self.client)
        access = login(self.client).data["access"]
        enroll_and_confirm(self.client, access)
        # Reuse the SAME pre-enrollment token, no re-login.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.get(f"/api/profile/{sid()}/")
        # Registration now creates the StudentProfile row, so the gate passes
        # and the (blank) profile is returned.
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_login_enrolled_user_gets_mfa_token_not_direct_tokens(self):
        register(self.client)
        access = login(self.client).data["access"]
        enroll_and_confirm(self.client, access)
        self.client.credentials()
        resp = login(self.client)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["must_enroll_totp"])
        self.assertIn("mfa_token", resp.data)
        self.assertNotIn("access", resp.data)

    def test_verify_totp_success_issues_tokens(self):
        register(self.client)
        access = login(self.client).data["access"]
        _, _, secret = enroll_and_confirm(self.client, access)
        self.client.credentials()
        mfa_token = login(self.client).data["mfa_token"]
        code = pyotp.TOTP(secret).now()
        resp = self.client.post("/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": code}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)

    def test_verify_totp_wrong_code_rejected(self):
        register(self.client)
        access = login(self.client).data["access"]
        enroll_and_confirm(self.client, access)
        self.client.credentials()
        mfa_token = login(self.client).data["mfa_token"]
        resp = self.client.post("/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": "000000"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_totp_throttled_after_max_attempts(self):
        register(self.client)
        access = login(self.client).data["access"]
        _, _, secret = enroll_and_confirm(self.client, access)
        self.client.credentials()
        mfa_token = login(self.client).data["mfa_token"]
        for _ in range(5):
            self.client.post("/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": "000000"}, format="json")
        code = pyotp.TOTP(secret).now()
        resp = self.client.post("/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": code}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_verify_totp_expired_mfa_token_rejected(self):
        register(self.client)
        access = login(self.client).data["access"]
        enroll_and_confirm(self.client, access)
        self.client.credentials()
        resp = self.client.post(
            "/api/auth/verify-totp/", {"mfa_token": "not-a-real-token", "code": "123456"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_verify_totp_backup_code_accepted_once(self):
        register(self.client)
        access = login(self.client).data["access"]
        _, verify_resp, _ = enroll_and_confirm(self.client, access)
        backup_code = verify_resp.data["backup_codes"][0]

        self.client.credentials()
        mfa_token = login(self.client).data["mfa_token"]
        resp1 = self.client.post(
            "/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": backup_code}, format="json"
        )
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        mfa_token_2 = login(self.client).data["mfa_token"]
        resp2 = self.client.post(
            "/api/auth/verify-totp/", {"mfa_token": mfa_token_2, "code": backup_code}, format="json"
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_wrong_password_rejected(self):
        register(self.client)
        resp = login(self.client, password="wrong-password")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_and_logout_flow(self):
        register(self.client)
        access = login(self.client).data["access"]
        _, _, secret = enroll_and_confirm(self.client, access)
        self.client.credentials()
        mfa_token = login(self.client).data["mfa_token"]
        code = pyotp.TOTP(secret).now()
        tokens = self.client.post(
            "/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": code}, format="json"
        ).data

        refresh_resp = self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refresh_resp.status_code, status.HTTP_200_OK)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        logout_resp = self.client.post("/api/auth/logout/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(logout_resp.status_code, status.HTTP_205_RESET_CONTENT)

        refresh_after_logout = self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refresh_after_logout.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_allowed_while_restricted(self):
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.post("/api/auth/logout/", {"refresh": "irrelevant"}, format="json")
        # Not blocked by the TOTP gate -- rejected only because "irrelevant" isn't a real refresh token.
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_me_endpoint_reports_role_and_totp_status(self):
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = self.client.get("/api/auth/me/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["role"], "student")
        self.assertFalse(resp.data["totp_enrolled"])

        enroll_and_confirm(self.client, access)
        resp2 = self.client.get("/api/auth/me/")
        self.assertTrue(resp2.data["totp_enrolled"])

    def test_me_endpoint_reports_onboarding_status(self):
        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        enroll_and_confirm(self.client, access)

        resp = self.client.get("/api/auth/me/")
        onboarding = resp.data["onboarding"]
        # The profile row is created at registration now; the remaining
        # onboarding steps are still outstanding.
        self.assertTrue(onboarding["profile_exists"])
        self.assertFalse(onboarding["resume_uploaded"])
        self.assertFalse(onboarding["github_connected"])
        self.assertFalse(onboarding["linkedin_connected"])
        self.assertFalse(onboarding["setup_complete"])

        self.client.post(
            "/api/profile/",
            {"github": "https://github.com/octocat", "linkedin_url": "https://linkedin.com/in/octocat"},
            format="json",
        )

        resp2 = self.client.get("/api/auth/me/")
        onboarding2 = resp2.data["onboarding"]
        self.assertTrue(onboarding2["profile_exists"])
        self.assertTrue(onboarding2["github_connected"])
        self.assertTrue(onboarding2["linkedin_connected"])
        self.assertFalse(onboarding2["resume_uploaded"])
        self.assertFalse(onboarding2["setup_complete"])  # resume still missing

    @mock.patch("agents.linkedin_agent.LinkedInAgent")
    def test_onboarding_reports_linkedin_connected_after_image_upload(self, MockLinkedInAgent):
        # LinkedIn is captured via image upload + parsing, not a typed URL --
        # linkedin_connected must not depend on profile.linkedin_url alone.
        MockLinkedInAgent.return_value.extract.return_value = {"skills": []}

        register(self.client)
        access = login(self.client).data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        enroll_and_confirm(self.client, access)

        image = SimpleUploadedFile("screenshot.png", b"fake-image-bytes", content_type="image/png")
        upload_resp = self.client.post("/api/profile/linkedin/", {"images": image}, format="multipart")
        self.assertEqual(upload_resp.status_code, status.HTTP_200_OK)

        resp = self.client.get("/api/auth/me/")
        self.assertTrue(resp.data["onboarding"]["linkedin_connected"])


def _extract_otp(email_message) -> str:
    import re

    match = re.search(r"code is: (\d{6})", email_message.body)
    assert match, f"no OTP found in email body: {email_message.body!r}"
    return match.group(1)


class ForgotPasswordFlowTests(TestCase):
    """
    Covers the self-service email-OTP password reset added to accounts.
    Deliberately exercises a university-role account (not just student) to
    confirm the flow is role-agnostic, per accounts.models.Account -- there
    is no separate university login to special-case.
    """

    def setUp(self):
        self.client = APIClient()
        cache.clear()
        mail.outbox = []

    def _make_user(self, email="student1@example.com", password="S3curePassw0rd!", role=Account.Role.STUDENT):
        from django_api.models import StudentProfile
        from universities.models import University

        user = User.objects.create_user(username=email, email=email, password=password)
        Account.objects.create(
            user=user,
            role=role,
            student_profile=(
                StudentProfile.objects.create(email=email)
                if role == Account.Role.STUDENT
                else None
            ),
            university=(
                University.objects.create(name="Western State University")
                if role == Account.Role.UNIVERSITY
                else None
            ),
        )
        return user

    def test_forgot_password_sends_otp_email_for_existing_user(self):
        self._make_user()
        resp = self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["student1@example.com"])

    def test_forgot_password_generic_response_for_unknown_email(self):
        resp = self.client.post("/api/auth/forgot-password/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_password_response_identical_for_known_and_unknown_email(self):
        self._make_user()
        known_resp = self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        cache.clear()
        unknown_resp = self.client.post("/api/auth/forgot-password/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(known_resp.data, unknown_resp.data)
        self.assertEqual(known_resp.status_code, unknown_resp.status_code)

    def test_forgot_password_resend_cooldown_suppresses_second_email(self):
        self._make_user()
        self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        resp2 = self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)  # second call was cooldown-suppressed, not a resend

    def test_full_reset_flow_changes_password_and_revokes_sessions(self):
        user = self._make_user(role=Account.Role.UNIVERSITY, email="admin@wsu.edu")

        # Establish an active session (refresh token) before the reset, to
        # verify it gets blacklisted afterwards.
        from rest_framework_simplejwt.tokens import RefreshToken

        refresh = RefreshToken.for_user(user)

        self.client.post("/api/auth/forgot-password/", {"email": "admin@wsu.edu"}, format="json")
        otp = _extract_otp(mail.outbox[0])

        verify_resp = self.client.post(
            "/api/auth/reset-password/verify-otp/", {"email": "admin@wsu.edu", "otp": otp}, format="json"
        )
        self.assertEqual(verify_resp.status_code, status.HTTP_200_OK)
        reset_token = verify_resp.data["reset_token"]

        confirm_resp = self.client.post(
            "/api/auth/reset-password/confirm/",
            {"reset_token": reset_token, "new_password": "N3wS3curePassw0rd!"},
            format="json",
        )
        self.assertEqual(confirm_resp.status_code, status.HTTP_200_OK)

        # Old password no longer works, new one does.
        old_login = self.client.post(
            "/api/auth/login/", {"email": "admin@wsu.edu", "password": "S3curePassw0rd!"}, format="json"
        )
        self.assertEqual(old_login.status_code, status.HTTP_401_UNAUTHORIZED)
        new_login = self.client.post(
            "/api/auth/login/", {"email": "admin@wsu.edu", "password": "N3wS3curePassw0rd!"}, format="json"
        )
        self.assertEqual(new_login.status_code, status.HTTP_200_OK)

        # Pre-reset refresh token was revoked.
        refresh_resp = self.client.post("/api/auth/refresh/", {"refresh": str(refresh)}, format="json")
        self.assertEqual(refresh_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # A confirmation email was sent, and the OTP/reset_token can't be replayed.
        self.assertEqual(len(mail.outbox), 2)
        replay_resp = self.client.post(
            "/api/auth/reset-password/confirm/",
            {"reset_token": reset_token, "new_password": "AnotherPassw0rd!"},
            format="json",
        )
        self.assertEqual(replay_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_verify_otp_wrong_code_rejected(self):
        self._make_user()
        self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        resp = self.client.post(
            "/api/auth/reset-password/verify-otp/",
            {"email": "student1@example.com", "otp": "000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify_otp_locked_after_max_attempts(self):
        self._make_user()
        self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        for _ in range(5):
            self.client.post(
                "/api/auth/reset-password/verify-otp/",
                {"email": "student1@example.com", "otp": "000000"},
                format="json",
            )
        otp = _extract_otp(mail.outbox[0])
        resp = self.client.post(
            "/api/auth/reset-password/verify-otp/", {"email": "student1@example.com", "otp": otp}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_verify_otp_cannot_be_reused(self):
        self._make_user()
        self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        otp = _extract_otp(mail.outbox[0])
        first = self.client.post(
            "/api/auth/reset-password/verify-otp/", {"email": "student1@example.com", "otp": otp}, format="json"
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.post(
            "/api/auth/reset-password/verify-otp/", {"email": "student1@example.com", "otp": otp}, format="json"
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_confirm_rejects_expired_or_unknown_token(self):
        resp = self.client.post(
            "/api/auth/reset-password/confirm/",
            {"reset_token": "not-a-real-token", "new_password": "N3wS3curePassw0rd!"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_reset_confirm_enforces_password_validation(self):
        self._make_user()
        self.client.post("/api/auth/forgot-password/", {"email": "student1@example.com"}, format="json")
        otp = _extract_otp(mail.outbox[0])
        reset_token = self.client.post(
            "/api/auth/reset-password/verify-otp/", {"email": "student1@example.com", "otp": otp}, format="json"
        ).data["reset_token"]

        resp = self.client.post(
            "/api/auth/reset-password/confirm/",
            {"reset_token": reset_token, "new_password": "short"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
