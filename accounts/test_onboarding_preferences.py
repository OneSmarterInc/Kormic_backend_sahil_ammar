from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from cryptography.fernet import Fernet
from rest_framework.test import APIClient
from accounts.models import Account, TOTPDevice
from accounts.serializers import student_onboarding_status
from django_api.models import StudentProfile

@override_settings(TOTP_SECRET_KEYS=(Fernet.generate_key().decode(),))
class OnboardingPreferenceTests(TestCase):
    def setUp(self):
        self.profile = StudentProfile.objects.create(name="Student", email="student@example.com")
        self.user = User.objects.create_user("student@example.com")
        self.account = Account.objects.create(user=self.user, role="student", student_profile=self.profile)
        TOTPDevice.objects.create(user=self.user, secret="JBSWY3DPEHPK3PXP", confirmed_at=timezone.now())
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def test_skip_survives_a_new_session_without_claiming_connection(self):
        response = self.client.patch("/api/auth/onboarding/preferences/", {"github_onboarding_state": "skipped", "linkedin_onboarding_state": "skipped"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.client.force_authenticate(User.objects.get(pk=self.user.pk))
        state = self.client.get("/api/auth/me/").json()["onboarding"]
        self.assertEqual(state["github_onboarding_state"], "skipped")
        self.assertEqual(state["linkedin_onboarding_state"], "skipped")
        self.assertFalse(state["github_connected"])
        self.assertFalse(state["linkedin_connected"])
        self.assertFalse(state["setup_complete"])

    def test_connection_overrides_skip_and_preference_can_be_reset(self):
        self.account.onboarding_preferences = {"github_onboarding_state": "skipped"}
        self.account.save()
        self.profile.github = "https://github.com/example"; self.profile.save()
        self.assertEqual(student_onboarding_status(self.profile.uuid)["github_onboarding_state"], "connected")
        self.profile.github = ""; self.profile.save()
        self.client.patch("/api/auth/onboarding/preferences/", {"github_onboarding_state": "required"}, format="json")
        self.assertEqual(student_onboarding_status(self.profile.uuid)["github_onboarding_state"], "required")

    def test_cannot_forge_connection_or_choose_another_student(self):
        for payload in [{"github_onboarding_state": "connected"}, {"student_id": "another", "github_onboarding_state": "skipped"}, {}]:
            self.assertEqual(self.client.patch("/api/auth/onboarding/preferences/", payload, format="json").status_code, 400)
        self.account.role = "university"; self.account.save()
        self.assertEqual(self.client.patch("/api/auth/onboarding/preferences/", {"github_onboarding_state": "skipped"}, format="json").status_code, 403)
