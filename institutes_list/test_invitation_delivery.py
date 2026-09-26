from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from institutes.services import register_institute
from institutes_list.invitation_email import claim_link, validate_delivery_url
from institutes_list.models import InstituteStudentList, ListedStudent
from institutes_list.tasks import send_invite_email_task
from institutes_list.management.commands.invitation_worker import work_once


class InvitationLinksTests(SimpleTestCase):
    @override_settings(EMAIL_MODE="prod", CLAIM_PAGE_URL="http://localhost:5173/claim")
    def test_live_smtp_can_use_the_configured_local_claim_url(self):
        validate_delivery_url()
        self.assertEqual(claim_link("test-code"), "http://localhost:5173/claim?token=test-code")

    def test_existing_query_is_preserved_and_old_token_replaced(self):
        link = claim_link("new+token", "https://student.example/claim?source=institute&token=old")
        self.assertEqual(parse_qs(urlsplit(link).query), {"source": ["institute"], "token": ["new+token"]})

    def test_invalid_configuration_is_rejected_before_queueing(self):
        for url in ("", "https://student.example", "javascript:alert(1)", "https://user:pass@example.com/claim", "http://student.example/claim", "https://student.example/claim#token=old"):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured):
                claim_link("example", url)

    @override_settings(STUDENT_WEB_CLAIM_URL="https://student.example/claim")
    def test_browser_bridge_preserves_code_without_sending_mail(self):
        response = self.client.get("/claim?token=abc%2B123")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://student.example/claim?token=abc%2B123")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    @override_settings(STUDENT_WEB_CLAIM_URL="http://localhost/claim")
    def test_browser_bridge_does_not_loop(self):
        response = self.client.get("/claim?token=abc", HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 200)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   CLAIM_PAGE_URL="https://app.kormic.ai/claim", CELERY_TASK_ALWAYS_EAGER=True,
                   INVITE_DELIVERY_MODE="database", DEFAULT_FROM_EMAIL="Team <hello@example.com>")
class InvitationDeliveryTests(TestCase):
    def setUp(self):
        institute = register_institute("Example <Institute>", country="IN", contact_email="staff@example.com")
        roster = InstituteStudentList.objects.create(institute=institute, contact_name="Staff", contact_email="staff@example.com")
        self.row = ListedStudent.objects.create(source_list=roster, institute_id=str(institute.uuid),
            full_name="Alex <script>alert(1)</script>", email="student@example.com",
            invited_at=timezone.now(), invite_delivery_status="queued")
        mail.outbox = []

    def test_multipart_invite_has_green_brand_institute_and_matching_code(self):
        self.assertTrue(work_once())
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.from_email, "Kormic <hello@example.com>")
        self.assertEqual(message.to, [self.row.email])
        html, mime = message.alternatives[0]
        self.assertEqual(mime, "text/html")
        self.assertIn("#385a46", html)
        self.assertIn("Example &lt;Institute&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("YOUR INVITATION CODE", html)
        self.assertIn(self.row.claim_token, html)
        self.assertIn(claim_link(self.row.claim_token), message.body)
        self.assertIn("Sent by Example <Institute> through Kormic", message.body)
        self.row.refresh_from_db()
        self.assertEqual(self.row.invite_delivery_status, "sent")
        self.assertIsNotNone(self.row.invite_delivered_at)
        self.assertFalse(work_once())

    @patch("institutes_list.tasks.send_mail", return_value=0)
    def test_backend_refusal_is_failed_not_sent(self, mocked):
        work_once()
        self.row.refresh_from_db()
        self.assertEqual(self.row.invite_delivery_status, "failed")
        self.assertIsNone(self.row.invite_delivered_at)
        self.assertFalse(work_once())

    @patch("institutes_list.tasks.send_mail", side_effect=ConnectionError("SMTP unavailable"))
    def test_smtp_failure_is_saved_for_manual_retry(self, mocked):
        work_once()
        self.row.refresh_from_db()
        self.assertEqual(self.row.invite_delivery_status, "failed")
        self.assertIn("SMTP unavailable", self.row.invite_delivery_error)

    def test_live_worker_lease_is_not_claimed_but_crashed_lease_recovers(self):
        ListedStudent.objects.filter(pk=self.row.pk).update(invite_delivery_started_at=timezone.now())
        self.assertFalse(work_once())
        ListedStudent.objects.filter(pk=self.row.pk).update(invite_delivery_started_at=timezone.now()-timedelta(minutes=6))
        self.assertTrue(work_once())
        self.assertEqual(len(mail.outbox), 1)

    def test_revoked_invitation_does_not_send(self):
        ListedStudent.objects.filter(pk=self.row.pk).update(status="revoked")
        work_once()
        self.assertEqual(len(mail.outbox), 0)

    def test_delayed_duplicate_task_does_not_resend_successful_invitation(self):
        work_once()
        send_invite_email_task(self.row.pk)
        self.assertEqual(len(mail.outbox), 1)
