from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Account
from django_api.models import StudentProfile


class ClaimProfileRegistrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.email = "claimed.student@example.com"
        self.password = "S3curePassw0rd!"

    def _register(self, email=None, name="Claimed Student"):
        return self.client.post(
            "/api/auth/register/",
            {
                "email": email or self.email,
                "password": self.password,
                "role": "student",
                "name": name,
            },
            format="json",
        )

    def test_registration_reuses_unowned_institute_claim_profile(self):
        claimed_profile = StudentProfile.objects.create(
            name="Institute Confirmed Name",
            email="Claimed.Student@example.com",
            institution="Feeder Institute",
            major="Computer Science",
            extra_data={
                "institute_sourced": {
                    "list_id": 17,
                    "institute_id": "inst-123",
                    "institute_name": "Feeder Institute",
                    "claimed_at": "2026-09-17T00:00:00+00:00",
                    "divergence_count": 0,
                }
            },
        )

        response = self._register()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        account = Account.objects.get(user__email=self.email)
        self.assertEqual(account.student_profile_id, claimed_profile.id)
        self.assertEqual(response.data["user"]["student_id"], str(claimed_profile.uuid))
        self.assertEqual(StudentProfile.objects.filter(email__iexact=self.email).count(), 1)

        claimed_profile.refresh_from_db()
        self.assertEqual(claimed_profile.name, "Institute Confirmed Name")
        self.assertEqual(claimed_profile.institution, "Feeder Institute")
        self.assertEqual(claimed_profile.major, "Computer Science")
        self.assertTrue(claimed_profile.extra_data["claimed_from_institute"])
        self.assertIn("institute_sourced", claimed_profile.extra_data)

    def test_registration_fails_closed_for_ambiguous_claim_profiles(self):
        provenance = {
            "institute_sourced": {
                "list_id": 17,
                "institute_id": "inst-123",
                "institute_name": "Feeder Institute",
            }
        }
        StudentProfile.objects.create(email=self.email, extra_data=provenance)
        StudentProfile.objects.create(email=self.email, extra_data=provenance)

        response = self._register()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email__iexact=self.email).exists())
        self.assertFalse(Account.objects.filter(user__email__iexact=self.email).exists())
        self.assertEqual(StudentProfile.objects.filter(email__iexact=self.email).count(), 2)

    def test_registration_refuses_unowned_non_claim_profile_instead_of_duplicating(self):
        orphan = StudentProfile.objects.create(
            name="Legacy Orphan",
            email=self.email,
            extra_data={},
        )

        response = self._register()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email__iexact=self.email).exists())
        self.assertEqual(StudentProfile.objects.filter(email__iexact=self.email).count(), 1)
        self.assertTrue(StudentProfile.objects.filter(id=orphan.id).exists())

    def test_registration_rejects_email_already_owned_by_student_identity(self):
        existing_profile = StudentProfile.objects.create(email=self.email)
        existing_user = User.objects.create_user(
            username="different-login@example.com",
            email="different-login@example.com",
            password=self.password,
        )
        Account.objects.create(
            user=existing_user,
            role=Account.Role.STUDENT,
            student_profile=existing_profile,
        )

        response = self._register()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email__iexact=self.email).exists())
        self.assertEqual(Account.objects.filter(role=Account.Role.STUDENT).count(), 1)
