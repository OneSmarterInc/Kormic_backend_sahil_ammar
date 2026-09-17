from __future__ import annotations

import hashlib
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Account
from institutes.services import register_institute
from institutes_list.models import ListedStudent, UniversityStudentList
from institutes_list.tasks import (
    claim_otp_cache_key,
    send_claim_otp_email_task,
)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ClaimOtpDeliveryTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.client = APIClient()
        self.institute = register_institute(
            "Indian Institute of Technology, Kanpur",
            contact_email="info@iitk.example",
        )
        self.source_list = UniversityStudentList.objects.create(
            institute=self.institute,
            contact_name="Admissions Office",
            contact_email="info@iitk.example",
        )
        self.student = ListedStudent.objects.create(
            source_list=self.source_list,
            institute_id=str(self.institute.uuid),
            full_name="Mike Student",
            email="mike@example.edu",
            field_of_study="Computer Science",
            degree_level="Bachelor's",
            expected_graduation="05/2027",
        )

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_canonical_start_endpoint_queues_delivery_and_returns_json(self, mock_delay):
        response = self.client.post(
            "/api/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(response.json(), {"masked_email": "m•••••@example.edu"})

        self.student.refresh_from_db()
        mock_delay.assert_called_once_with(self.student.id, self.student.otp_hash)
        self.assertEqual(self.student.otp_attempts, 0)
        self.assertIsNotNone(self.student.otp_expires_at)

        code = cache.get(claim_otp_cache_key(self.student.id, self.student.otp_hash))
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(hashlib.sha256(code.encode("utf-8")).hexdigest(), self.student.otp_hash)
        self.assertEqual(mail.outbox, [])

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_compatibility_endpoint_supports_clients_missing_api_prefix(self, mock_delay):
        response = self.client.post(
            "/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        mock_delay.assert_called_once()

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_broker_failure_returns_json_and_invalidates_unsent_code(self, mock_delay):
        mock_delay.side_effect = RuntimeError("broker unavailable")

        response = self.client.post(
            "/api/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(
            response.json(),
            {"error": "Verification code could not be sent. Please try again."},
        )

        self.student.refresh_from_db()
        self.assertEqual(self.student.otp_hash, "")
        self.assertIsNone(self.student.otp_expires_at)
        self.assertEqual(self.student.otp_attempts, 0)

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_task_delivers_current_code_and_removes_plaintext_cache_value(self, mock_delay):
        response = self.client.post(
            "/api/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        task_args = mock_delay.call_args.args
        self.student.refresh_from_db()
        cache_key = claim_otp_cache_key(self.student.id, self.student.otp_hash)
        code = cache.get(cache_key)

        send_claim_otp_email_task(*task_args)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.student.email])
        self.assertIn(code, mail.outbox[0].body)
        self.assertIsNone(cache.get(cache_key))

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_delayed_task_for_replaced_code_cannot_email_stale_otp(self, mock_delay):
        first = self.client.post(
            "/api/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )
        self.assertEqual(first.status_code, 200)
        first_args = mock_delay.call_args.args

        second = self.client.post(
            "/api/claim/start/",
            {"token": self.student.claim_token},
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        second_args = mock_delay.call_args.args

        send_claim_otp_email_task(*first_args)
        self.assertEqual(mail.outbox, [])

        send_claim_otp_email_task(*second_args)
        self.assertEqual(len(mail.outbox), 1)

    def test_unknown_token_still_uses_generic_json_error(self):
        response = self.client.post(
            "/api/claim/start/",
            {"token": "not-a-real-invitation"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(
            response.json(),
            {"error": "No claimable invitation found for that information."},
        )


class ClaimOtpRouteIsolationTests(TestCase):
    """The public claim flow must not require an authenticated account."""

    @mock.patch("institutes_list.claim_views.send_claim_otp_email_task.delay")
    def test_anonymous_claim_start_remains_allowed(self, mock_delay):
        cache.clear()
        institute = register_institute("Anonymous Claim Institute")
        source_list = UniversityStudentList.objects.create(
            institute=institute,
            contact_name="Contact",
            contact_email="contact@example.edu",
        )
        listed = ListedStudent.objects.create(
            source_list=source_list,
            institute_id=str(institute.uuid),
            full_name="Anonymous Student",
            email="anonymous@example.edu",
        )

        # Create an unrelated authenticated user to ensure no implicit role
        # dependency is introduced by the new route implementation.
        user = get_user_model().objects.create_user(
            username="officer@example.edu",
            email="officer@example.edu",
            password="test-password",
        )
        Account.objects.create(user=user, role=Account.Role.INSTITUTE, institute=institute)

        response = APIClient().post(
            "/api/claim/start/",
            {"token": listed.claim_token},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once()
