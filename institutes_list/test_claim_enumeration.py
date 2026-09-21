from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from institutes.services import register_institute
from institutes_list.models import ListedStudent, InstituteStudentList


class ClaimEnumerationResistanceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        institute = register_institute("Enumeration Safe Institute", country="IN")
        source_list = InstituteStudentList.objects.create(
            institute=institute,
            contact_name="Admissions",
            contact_email="admissions@example.edu",
        )
        self.listed = ListedStudent.objects.create(
            source_list=source_list,
            institute_id=str(institute.uuid),
            full_name="Listed Student",
            email="listed@example.edu",
        )

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_listed_and_unlisted_start_are_identical(self, _mock_delay):
        listed = self.client.post(
            "/api/claim/start/", {"email": "listed@example.edu"}, format="json"
        )
        unlisted = self.client.post(
            "/api/claim/start/", {"email": "nobody@example.edu"}, format="json"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(unlisted.status_code, listed.status_code)
        self.assertEqual(unlisted.json(), listed.json())
        self.assertEqual(listed.json(), {"sent": True})
        _mock_delay.assert_called_once()

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_listed_and_unlisted_verify_failures_are_identical(self, _mock_delay):
        self.client.post(
            "/api/claim/start/", {"email": "listed@example.edu"}, format="json"
        )
        listed = self.client.post(
            "/api/claim/verify/",
            {"email": "listed@example.edu", "code": "000000"},
            format="json",
        )
        unlisted = self.client.post(
            "/api/claim/verify/",
            {"email": "nobody@example.edu", "code": "000000"},
            format="json",
        )
        self.assertEqual(listed.status_code, 400)
        self.assertEqual(unlisted.status_code, listed.status_code)
        self.assertEqual(unlisted.json(), listed.json())
        self.assertEqual(listed.json(), {"error": "That code didn't work"})

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_unknown_wrong_expired_and_locked_verify_are_indistinguishable(self, _mock_delay):
        self.client.post(
            "/api/claim/start/", {"email": "listed@example.edu"}, format="json"
        )
        self.listed.refresh_from_db()

        wrong = self.client.post(
            "/api/claim/verify/",
            {"email": "listed@example.edu", "code": "000000"},
            format="json",
        )
        unknown = self.client.post(
            "/api/claim/verify/",
            {"email": "nobody@example.edu", "code": "000000"},
            format="json",
        )

        self.listed.otp_expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.listed.save(update_fields=["otp_expires_at"])
        expired = self.client.post(
            "/api/claim/verify/",
            {"email": "listed@example.edu", "code": "000000"},
            format="json",
        )

        self.listed.otp_expires_at = timezone.now() + timezone.timedelta(minutes=5)
        self.listed.otp_attempts = 5
        self.listed.save(update_fields=["otp_expires_at", "otp_attempts"])
        locked = self.client.post(
            "/api/claim/verify/",
            {"email": "listed@example.edu", "code": "000000"},
            format="json",
        )

        expected = (400, {"error": "That code didn't work"})
        for response in (wrong, unknown, expired, locked):
            self.assertEqual((response.status_code, response.json()), expected)

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_six_wrong_attempts_are_identical_for_listed_and_unlisted(self, _mock_delay):
        self.client.post(
            "/api/claim/start/", {"email": "listed@example.edu"}, format="json"
        )

        for _ in range(6):
            listed = self.client.post(
                "/api/claim/verify/",
                {"email": "listed@example.edu", "code": "000000"},
                format="json",
            )
            unlisted = self.client.post(
                "/api/claim/verify/",
                {"email": "nobody@example.edu", "code": "000000"},
                format="json",
            )
            self.assertEqual(listed.status_code, 400)
            self.assertEqual(unlisted.status_code, listed.status_code)
            self.assertEqual(unlisted.json(), listed.json())
            self.assertEqual(listed.json(), {"error": "That code didn't work"})

        self.listed.refresh_from_db()
        self.assertEqual(self.listed.otp_attempts, 5)
