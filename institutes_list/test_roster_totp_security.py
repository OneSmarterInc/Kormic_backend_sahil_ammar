from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework.test import APIClient

from accounts.models import Account
from institutes.services import register_institute


class InstituteRosterTOTPTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        institute = register_institute("TOTP Gate Institute")
        user = get_user_model().objects.create_user(
            username="preenrollment@institute.test",
            email="preenrollment@institute.test",
            password="StrongPassword123!",
        )
        Account.objects.create(
            user=user,
            role=Account.Role.INSTITUTE,
            institute=institute,
        )
        token = str(AccessToken.for_user(user))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_pre_enrollment_token_is_forbidden_on_every_roster_endpoint(self):
        probes = (
            ("post", "/api/institute-lists/upload/", {}),
            ("get", "/api/institute-lists/lists/", None),
            ("get", "/api/institute-lists/lists/999999/students/", None),
            ("get", "/api/institute-lists/lists/999999/file/", None),
            ("post", "/api/institute-lists/lists/999999/send-invites/", {}),
            ("post", "/api/institute-lists/lists/999999/students/999999/send-invite/", {}),
        )
        for method, path, data in probes:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, data=data, format="json")
                self.assertEqual(response.status_code, 403)
                self.assertIn("TOTP", str(response.data))
