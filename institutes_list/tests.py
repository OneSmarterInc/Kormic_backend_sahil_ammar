"""
Claim-flow tests. The adversarial ones mirror the spec's security core:
possession of a token reveals nothing beyond a masked email; wrong codes are
counted and limited; a claimed invitation is dead; edits are recorded as
divergences; re-uploads never overwrite claimed rows.
"""
import io
import re
from copy import deepcopy
from unittest import mock

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account
from django_api.models import StudentProfile
from institutes.services import register_institute

from .models import ListedStudent, UniversityStudentList
from .tasks import claim_otp_cache_key

CSV = (
    "full_name,email,field_of_study,degree_level,expected_graduation,phone\n"
    "Priya Sharma,priya.sharma@gmail.com,Computer Engineering,Masters,05/2027,+91 90000 00001\n"
    "Arjun Rao,arjun.rao@gmail.com,Data Science,Masters,05/2027,\n"
    "Bad Row,not-an-email,CS,Masters,05/2027,\n"
)


def _code_from_outbox() -> str:
    # Legacy tests used to read the code from locmem email because
    # claim/start sent SMTP synchronously. Delivery is now queued, so the
    # view stores the short-lived plaintext code in the shared cache and the
    # Celery task removes it after sending. Keep the helper name to avoid
    # obscuring the behavioral tests below, but read the queued code when no
    # email has been delivered in-process.
    if mail.outbox:
        body = mail.outbox[-1].body
        return re.search(r"(\d{6})", body).group(1)

    row = ListedStudent.objects.exclude(otp_hash="").order_by("-otp_expires_at").first()
    if row is None:
        raise AssertionError("No claim OTP was prepared.")

    code = cache.get(claim_otp_cache_key(row.id, row.otp_hash))
    if not isinstance(code, str) or not code:
        raise AssertionError("The queued claim OTP is unavailable.")
    return code


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ClaimFlowTests(TestCase):
    def setUp(self):
        # B3's rate limits are keyed in the process-wide cache, which
        # (unlike the DB) isn't rolled back between test methods -- clear it
        # so one method's claim/start calls don't eat into another's budget.
        cache.clear()
        self.claim_otp_delivery_patcher = mock.patch(
            "institutes_list.claim_views.send_claim_otp_email_task.delay"
        )
        self.mock_claim_otp_delivery = self.claim_otp_delivery_patcher.start()
        self.addCleanup(self.claim_otp_delivery_patcher.stop)

        self.client = APIClient()
        self.institute = register_institute("Wright State Feeder Institute", contact_email="ops@wsfi.edu")
        user = get_user_model().objects.create_user(
            username="officer@wsfi.edu", email="officer@wsfi.edu", password="x"
        )
        Account.objects.create(user=user, role=Account.Role.INSTITUTE, institute=self.institute)
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/institute-lists/upload/",
            {
                "file": io.BytesIO(CSV.encode()),
                "institute_id": str(self.institute.uuid),
                "contact_name": "Dr. John",
                "contact_email": "john@wsfi.edu",
                "contact_verification": "institutional email domain",
            },
            format="multipart",
        )
        self.upload = resp.json()
        self.client.force_authenticate(user=None)  # claim side is anonymous

    # ------------------------------------------------------------------ intake

    def test_upload_ingests_with_provenance_and_rejects_bad_rows(self):
        self.assertEqual(self.upload["accepted"], 2)
        self.assertEqual(len(self.upload["rejected"]), 1)
        lst = UniversityStudentList.objects.get(id=self.upload["list_id"])
        self.assertEqual(lst.contact_name, "Dr. John")
        self.assertEqual(lst.row_count, 2)
        self.assertEqual(
            ListedStudent.objects.filter(status=ListedStudent.Status.UNCLAIMED).count(), 2
        )

    # ------------------------------------------------------------- claim: start

    def test_start_reveals_only_masked_email(self):
        row = ListedStudent.objects.get(email="priya.sharma@gmail.com")
        resp = self.client.post("/api/claim/start/", {"token": row.claim_token}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"masked_email": "p•••••@gmail.com"})

        row.refresh_from_db()
        self.mock_claim_otp_delivery.assert_called_once_with(row.id, row.otp_hash)
        self.assertEqual(mail.outbox, [])

    def test_start_is_generic_for_unknown_email(self):
        resp = self.client.post("/api/claim/start/", {"email": "stranger@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("stranger", str(resp.json()))

    # ------------------------------------------------------------ claim: verify

    def test_wrong_code_counted_and_limited(self):
        self.client.post("/api/claim/start/", {"email": "priya.sharma@gmail.com"}, format="json")
        for _ in range(5):
            resp = self.client.post(
                "/api/claim/verify/",
                {"email": "priya.sharma@gmail.com", "code": "000000"},
                format="json",
            )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/claim/verify/", {"email": "priya.sharma@gmail.com", "code": "000000"}, format="json"
        )
        self.assertEqual(resp.status_code, 429)

    def test_correct_code_returns_prefill(self):
        self.client.post("/api/claim/start/", {"email": "priya.sharma@gmail.com"}, format="json")
        resp = self.client.post(
            "/api/claim/verify/",
            {"email": "priya.sharma@gmail.com", "code": _code_from_outbox()},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["prefill"]["full_name"], "Priya Sharma")
        self.assertEqual(data["prefill"]["institute_name"], "Wright State Feeder Institute")
        self.assertIn("claim_session", data)

    # ----------------------------------------------------------- claim: confirm

    def _verified_session(self, email="priya.sharma@gmail.com"):
        self.client.post("/api/claim/start/", {"email": email}, format="json")
        resp = self.client.post(
            "/api/claim/verify/", {"email": email, "code": _code_from_outbox()}, format="json"
        )
        return resp.json()["claim_session"]

    def test_confirm_records_divergence_and_creates_sourced_profile(self):
        session = self._verified_session()
        resp = self.client.post(
            "/api/claim/confirm/",
            {
                "claim_session": session,
                "fields": {"field_of_study": "Computer Science", "email": "attacker@evil.com"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["divergences_recorded"], 1)
        self.assertTrue(data["badge"]["institute_sourced"])

        row = ListedStudent.objects.get(email="priya.sharma@gmail.com")
        self.assertEqual(row.status, ListedStudent.Status.CLAIMED)
        self.assertEqual(row.divergences[0]["field"], "field_of_study")

        profile = StudentProfile.objects.get(email__iexact="priya.sharma@gmail.com")
        # the claim address was NOT editable -- attacker@evil.com ignored
        self.assertEqual(profile.email, "priya.sharma@gmail.com")
        self.assertEqual(profile.major, "Computer Science")
        self.assertEqual(profile.extra_data["institute_sourced"]["institute_id"], str(self.institute.uuid))

    def test_claimed_invitation_is_dead(self):
        session = self._verified_session()
        self.client.post("/api/claim/confirm/", {"claim_session": session, "fields": {}}, format="json")
        # token now dead for start...
        row = ListedStudent.objects.get(email="priya.sharma@gmail.com")
        resp = self.client.post("/api/claim/start/", {"token": row.claim_token}, format="json")
        self.assertEqual(resp.status_code, 404)
        # ...and the session cannot confirm twice
        resp = self.client.post("/api/claim/confirm/", {"claim_session": session, "fields": {}}, format="json")
        self.assertEqual(resp.status_code, 400)

    # -------------------------------------------------------------- reconciliation

    def test_reupload_never_overwrites_claimed(self):
        session = self._verified_session()
        self.client.post("/api/claim/confirm/", {"claim_session": session, "fields": {}}, format="json")

        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        changed = CSV.replace("Priya Sharma", "Priya CHANGED")
        resp = self.client.post(
            "/api/institute-lists/upload/",
            {
                "file": io.BytesIO(changed.encode()),
                "institute_id": str(self.institute.uuid),
                "contact_name": "Dr. John",
                "contact_email": "john@wsfi.edu",
            },
            format="multipart",
        )
        data = resp.json()
        self.assertEqual(len(data["skipped_claimed"]), 1)
        profile = StudentProfile.objects.get(email__iexact="priya.sharma@gmail.com")
        self.assertEqual(profile.name, "Priya Sharma")  # untouched

    # -------------------------------------------------------------- ownership

    def test_institute_cannot_upload_for_a_different_institute(self):
        other = register_institute("Some Other Institute")
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/institute-lists/upload/",
            {
                "file": io.BytesIO(CSV.encode()),
                "institute_id": str(other.uuid),
                "contact_name": "Dr. John",
                "contact_email": "john@wsfi.edu",
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 403)

    # -------------------------------------------------------------- roster + invites

    def test_list_students_shows_roster_for_own_list(self):
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"/api/institute-lists/lists/{self.upload['list_id']}/students/")
        self.assertEqual(resp.status_code, 200)
        emails = {s["email"] for s in resp.json()["students"]}
        self.assertEqual(emails, {"priya.sharma@gmail.com", "arjun.rao@gmail.com"})

    def test_list_students_rejects_a_different_institute(self):
        other = register_institute("Some Other Institute")
        other_user = get_user_model().objects.create_user(
            username="officer@other.edu", email="officer@other.edu", password="x"
        )
        Account.objects.create(user=other_user, role=Account.Role.INSTITUTE, institute=other)
        self.client.force_authenticate(user=other_user)
        resp = self.client.get(f"/api/institute-lists/lists/{self.upload['list_id']}/students/")
        self.assertEqual(resp.status_code, 403)

    def test_list_lists_shows_claimed_and_unclaimed_counts(self):
        session = self._verified_session()
        self.client.post("/api/claim/confirm/", {"claim_session": session, "fields": {}}, format="json")

        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/institute-lists/lists/")
        self.assertEqual(resp.status_code, 200)
        lst = resp.json()["lists"][0]
        self.assertEqual(lst["claimed_count"], 1)
        self.assertEqual(lst["unclaimed_count"], 1)

    @override_settings(CLAIM_PAGE_URL="https://app.kormic.example/claim")
    @mock.patch("institutes_list.views.send_invite_email_task.delay")
    def test_send_invites_emails_unclaimed_rows_and_is_idempotent(self, mock_delay):
        # The actual SMTP send now happens in send_invite_email_task
        # (Celery), not inline -- see institutes_list/tasks.py. The view's
        # job is just to mark invited_at and queue one task per row, so
        # that's what this test verifies; send_invite_email_task itself is
        # covered separately below.
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)

        resp = self.client.post(f"/api/institute-lists/lists/{self.upload['list_id']}/send-invites/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["invites_sent"], 2)
        self.assertEqual(mock_delay.call_count, 2)

        queued_ids = {call.args[0] for call in mock_delay.call_args_list}
        self.assertEqual(
            queued_ids,
            set(ListedStudent.objects.filter(source_list_id=self.upload["list_id"]).values_list("id", flat=True)),
        )
        self.assertTrue(
            ListedStudent.objects.filter(source_list_id=self.upload["list_id"], invited_at__isnull=True).count() == 0
        )

        # calling again does not re-invite already-invited unclaimed rows
        mock_delay.reset_mock()
        resp = self.client.post(f"/api/institute-lists/lists/{self.upload['list_id']}/send-invites/")
        self.assertEqual(resp.json()["invites_sent"], 0)
        mock_delay.assert_not_called()

    @override_settings(CLAIM_PAGE_URL="https://app.kormic.example/claim")
    def test_send_invite_email_task_sends_claim_link(self):
        from institutes_list.tasks import send_invite_email_task

        mail.outbox = []
        row = ListedStudent.objects.filter(source_list_id=self.upload["list_id"]).first()

        send_invite_email_task(row.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://app.kormic.example/claim?token=", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [row.email])

    def test_send_invites_requires_claim_page_url_configured(self):
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        resp = self.client.post(f"/api/institute-lists/lists/{self.upload['list_id']}/send-invites/")
        self.assertEqual(resp.status_code, 500)

    @override_settings(CLAIM_PAGE_URL="https://app.kormic.example/claim")
    @mock.patch("institutes_list.views.send_invite_email_task.delay")
    def test_send_invite_resends_a_single_already_invited_row(self, mock_delay):
        # send_invites' bulk pass skips already-invited rows on purpose --
        # send_invite is the explicit "resend to just this one student"
        # action and must NOT skip, even though invited_at is already set.
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        row = ListedStudent.objects.filter(source_list_id=self.upload["list_id"]).first()
        row.invited_at = timezone.now()
        row.save(update_fields=["invited_at"])
        first_invited_at = row.invited_at

        resp = self.client.post(
            f"/api/institute-lists/lists/{self.upload['list_id']}/students/{row.id}/send-invite/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["student_id"], row.id)
        mock_delay.assert_called_once_with(row.id)

        row.refresh_from_db()
        self.assertGreater(row.invited_at, first_invited_at)

    @override_settings(CLAIM_PAGE_URL="https://app.kormic.example/claim")
    @mock.patch("institutes_list.views.send_invite_email_task.delay")
    def test_send_invite_rejects_a_claimed_row(self, mock_delay):
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        row = ListedStudent.objects.filter(source_list_id=self.upload["list_id"]).first()
        row.status = ListedStudent.Status.CLAIMED
        row.save(update_fields=["status"])

        resp = self.client.post(
            f"/api/institute-lists/lists/{self.upload['list_id']}/students/{row.id}/send-invite/"
        )
        self.assertEqual(resp.status_code, 400)
        mock_delay.assert_not_called()

    @override_settings(CLAIM_PAGE_URL="https://app.kormic.example/claim")
    def test_send_invite_404s_for_a_student_on_a_different_list(self):
        user = get_user_model().objects.get(username="officer@wsfi.edu")
        self.client.force_authenticate(user=user)
        other = register_institute("Some Other Institute")
        other_list = UniversityStudentList.objects.create(institute=other, contact_name="x", contact_email="x@x.com")
        other_row = ListedStudent.objects.create(
            source_list=other_list,
            institute_id=str(other.uuid),
            full_name="Other Student",
            email="other.student@gmail.com",
        )

        resp = self.client.post(
            f"/api/institute-lists/lists/{self.upload['list_id']}/students/{other_row.id}/send-invite/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_send_invite_rejects_a_different_institute(self):
        other = register_institute("Some Other Institute")
        other_user = get_user_model().objects.create_user(
            username="officer2@other.edu", email="officer2@other.edu", password="x"
        )
        Account.objects.create(user=other_user, role=Account.Role.INSTITUTE, institute=other)
        self.client.force_authenticate(user=other_user)
        row = ListedStudent.objects.filter(source_list_id=self.upload["list_id"]).first()

        resp = self.client.post(
            f"/api/institute-lists/lists/{self.upload['list_id']}/students/{row.id}/send-invite/"
        )
        self.assertEqual(resp.status_code, 403)


def _throttled_rest_framework() -> dict:
    """A copy of settings.REST_FRAMEWORK with tiny claim throttle rates so
    a test can exhaust a budget in a handful of calls instead of needing
    real load. ip/email rates are deliberately different sizes so a test can
    isolate which one tripped."""
    rates = deepcopy(django_settings.REST_FRAMEWORK)
    rates["DEFAULT_THROTTLE_RATES"] = {
        **rates["DEFAULT_THROTTLE_RATES"],
        "claim_start_ip": "4/min",
        "claim_start_email": "2/min",
        "claim_verify_ip": "4/min",
        "claim_verify_email": "2/min",
    }
    return rates


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    REST_FRAMEWORK=_throttled_rest_framework(),
)
class ClaimRateLimitTests(TestCase):
    """
    B3: claim/start and claim/verify are public by design (the OTP is the
    auth), so both need independent per-email and per-IP throttles so
    neither can be hammered.
    """

    def setUp(self):
        cache.clear()
        self.claim_otp_delivery_patcher = mock.patch(
            "institutes_list.claim_views.send_claim_otp_email_task.delay"
        )
        self.mock_claim_otp_delivery = self.claim_otp_delivery_patcher.start()
        self.addCleanup(self.claim_otp_delivery_patcher.stop)

        self.client = APIClient()
        self.institute = register_institute("Rate Limit Institute", contact_email="ops@rli.edu")
        user = get_user_model().objects.create_user(
            username="officer@rli.edu", email="officer@rli.edu", password="x"
        )
        Account.objects.create(user=user, role=Account.Role.INSTITUTE, institute=self.institute)
        self.client.force_authenticate(user=user)
        self.client.post(
            "/api/institute-lists/upload/",
            {
                "file": io.BytesIO(CSV.encode()),
                "institute_id": str(self.institute.uuid),
                "contact_name": "Dr. John",
                "contact_email": "john@rli.edu",
                "contact_verification": "institutional email domain",
            },
            format="multipart",
        )
        self.client.force_authenticate(user=None)

    def tearDown(self):
        cache.clear()

    def test_start_throttles_the_same_email_before_the_shared_ip_budget(self):
        for _ in range(2):
            resp = self.client.post("/api/claim/start/", {"email": "priya.sharma@gmail.com"}, format="json")
            self.assertEqual(resp.status_code, 200)

        resp = self.client.post("/api/claim/start/", {"email": "priya.sharma@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 429)

    def test_start_throttles_per_ip_across_different_emails(self):
        for email in ("priya.sharma@gmail.com", "arjun.rao@gmail.com", "unknown1@gmail.com", "unknown2@gmail.com"):
            resp = self.client.post("/api/claim/start/", {"email": email}, format="json")
            self.assertNotEqual(resp.status_code, 429)

        # a 5th distinct email from the same source exhausts the shared per-IP budget
        resp = self.client.post("/api/claim/start/", {"email": "unknown3@gmail.com"}, format="json")
        self.assertEqual(resp.status_code, 429)

    def test_verify_throttles_the_same_email_before_the_shared_ip_budget(self):
        self.client.post("/api/claim/start/", {"email": "priya.sharma@gmail.com"}, format="json")

        for _ in range(2):
            resp = self.client.post(
                "/api/claim/verify/", {"email": "priya.sharma@gmail.com", "code": "000000"}, format="json"
            )
            self.assertEqual(resp.status_code, 400)

        resp = self.client.post(
            "/api/claim/verify/", {"email": "priya.sharma@gmail.com", "code": "000000"}, format="json"
        )
        self.assertEqual(resp.status_code, 429)
