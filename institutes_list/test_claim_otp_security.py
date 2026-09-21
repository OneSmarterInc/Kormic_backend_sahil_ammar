from concurrent.futures import ThreadPoolExecutor
from unittest import skipIf

from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from institutes.services import register_institute
from institutes_list.models import ListedStudent, InstituteStudentList
from institutes_list.views import OTP_MAX_ATTEMPTS, OTP_TTL_SECONDS, _hash_otp


class ClaimOtpHashIsolationTests(TestCase):
    def setUp(self):
        institute = register_institute("OTP Hash Isolation Institute", country="IN")
        source_list = InstituteStudentList.objects.create(
            institute=institute,
            contact_name="Admissions",
            contact_email="admissions@example.edu",
        )
        self.first = ListedStudent.objects.create(
            source_list=source_list,
            institute_id=str(institute.uuid),
            full_name="First Student",
            email="first@example.edu",
        )
        self.second = ListedStudent.objects.create(
            source_list=source_list,
            institute_id=str(institute.uuid),
            full_name="Second Student",
            email="second@example.edu",
        )

    def test_same_code_on_two_rows_produces_different_hashes(self):
        code = "123456"
        first_hash = _hash_otp(self.first.id, code)
        second_hash = _hash_otp(self.second.id, code)

        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(first_hash, _hash_otp(self.first.id, code))
        self.assertEqual(second_hash, _hash_otp(self.second.id, code))


class ClaimOtpConcurrentAttemptTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        institute = register_institute("OTP Concurrency Institute")
        source_list = InstituteStudentList.objects.create(
            institute=institute,
            contact_name="Admissions",
            contact_email="admissions@example.edu",
        )
        self.student = ListedStudent.objects.create(
            source_list=source_list,
            institute_id=str(institute.uuid),
            full_name="Concurrent Student",
            email="concurrent@example.edu",
        )
        code = "123456"
        self.student.otp_hash = _hash_otp(self.student.id, code)
        self.student.otp_expires_at = timezone.now() + timezone.timedelta(
            seconds=OTP_TTL_SECONDS
        )
        self.student.otp_attempts = 0
        self.student.save(
            update_fields=["otp_hash", "otp_expires_at", "otp_attempts"]
        )

    def _wrong_guess(self):
        close_old_connections()
        try:
            client = APIClient()
            response = client.post(
                "/api/claim/verify/",
                {"token": self.student.claim_token, "code": "000000"},
                format="json",
            )
            return response.status_code
        finally:
            connections.close_all()

    @skipIf(connection.vendor == "sqlite", "SQLite serializes concurrent writers; PostgreSQL CI covers atomic lockout.")
    def test_five_parallel_wrong_guesses_lock_the_row(self):
        with ThreadPoolExecutor(max_workers=OTP_MAX_ATTEMPTS) as executor:
            statuses = list(
                executor.map(lambda _index: self._wrong_guess(), range(OTP_MAX_ATTEMPTS))
            )

        self.student.refresh_from_db()
        self.assertEqual(self.student.otp_attempts, OTP_MAX_ATTEMPTS)
        self.assertTrue(all(status in {400, 429} for status in statuses))

        response = APIClient().post(
            "/api/claim/verify/",
            {"token": self.student.claim_token, "code": "000000"},
            format="json",
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("too many attempts", str(response.data))
