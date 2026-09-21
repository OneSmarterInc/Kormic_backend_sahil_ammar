from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from institutes.services import register_institute
from institutes_list.models import ListedStudent, UniversityStudentList


class ClaimEnumerationResistanceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        institute = register_institute("Enumeration Safe Institute", country="IN")
        source_list = UniversityStudentList.objects.create(
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
