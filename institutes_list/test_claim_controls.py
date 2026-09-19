from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.core.cache import cache
from django.db import connection, close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from institutes.services import register_institute
from .models import UniversityStudentList, ListedStudent
from .otp import hash_otp
from .views import _claim_signer


class ClaimControlsTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        institute = register_institute('Claim Controls')
        source = UniversityStudentList.objects.create(institute=institute, contact_name='Admin', contact_email='admin@example.test')
        self.row = ListedStudent.objects.create(source_list=source, institute_id=str(institute.uuid), full_name='Ada', email='ada@example.test', otp_hash=hash_otp('123456'), otp_expires_at=timezone.now()+timezone.timedelta(minutes=10))
        self.client = APIClient()

    @patch('institutes_list.claim_views.send_claim_otp_email_task.delay')
    def test_start_status_and_body_do_not_disclose_membership_or_delivery(self, send):
        responses = []
        for payload in [{'email': self.row.email}, {'email': 'absent@example.test'}, {'token': self.row.claim_token}, {'token': 'absent'}]:
            response = self.client.post('/api/v1/claim/start/', payload, format='json')
            responses.append((response.status_code, response.json()))
        send.side_effect = RuntimeError('broker unavailable')
        r = self.client.post('/api/v1/claim/start/', {'email': self.row.email}, format='json')
        responses.append((r.status_code, r.json()))
        self.row.status = ListedStudent.Status.CLAIMED; self.row.save()
        r = self.client.post('/api/v1/claim/start/', {'token': self.row.claim_token}, format='json')
        responses.append((r.status_code, r.json()))
        self.assertTrue(all(value == responses[0] for value in responses), responses)
        self.assertEqual(responses[0][0], 200)
        self.assertNotIn(self.row.email, str(responses))

    def test_wrong_unknown_expired_and_exhausted_codes_are_indistinguishable(self):
        def verify(email):
            r = self.client.post('/api/v1/claim/verify/', {'email': email, 'code': '000000'}, format='json')
            # request_id is deliberately unique and not part of the error semantics.
            return r.status_code, r.json()['error']['code'], r.json()['error']['message']
        wrong = verify(self.row.email)
        self.assertEqual(verify('absent@example.test'), wrong)
        self.row.otp_attempts = 5; self.row.save()
        self.assertEqual(verify(self.row.email), wrong)
        self.row.otp_attempts = 0; self.row.otp_expires_at = timezone.now(); self.row.save()
        self.assertEqual(verify(self.row.email), wrong)

    @override_settings(REST_FRAMEWORK={'DEFAULT_THROTTLE_RATES': {'claim_confirm_ip': '100/min', 'claim_confirm_session': '2/min'}})
    def test_confirm_has_independent_session_limit(self):
        statuses = [self.client.post('/api/v1/claim/confirm/', {'claim_session': 'invalid'}, format='json', REMOTE_ADDR=f'198.51.100.{i}').status_code for i in range(3)]
        self.assertEqual(statuses, [400, 400, 429])

    @override_settings(REST_FRAMEWORK={'DEFAULT_THROTTLE_RATES': {'claim_confirm_ip': '2/min', 'claim_confirm_session': '100/min'}})
    def test_confirm_has_ip_limit_across_sessions(self):
        statuses = [self.client.post('/api/v1/claim/confirm/', {'claim_session': f'invalid-{i}'}, format='json').status_code for i in range(3)]
        self.assertEqual(statuses, [400, 400, 429])

    def parallel(self, count, action):
        if connection.vendor != 'postgresql': self.skipTest('Row locking requires PostgreSQL; exercised in CI')
        barrier = Barrier(count)
        def run(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action(i)
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=count) as pool: return list(pool.map(run, range(count)))

    def test_parallel_guesses_reserve_at_most_five_attempts(self):
        with patch('institutes_list.views.check_otp', return_value=False) as compare:
            statuses = self.parallel(8, lambda i: APIClient().post('/api/v1/claim/verify/', {'email': self.row.email, 'code': '000000'}, format='json').status_code)
            self.assertEqual(statuses, [400]*8)
            self.assertEqual(compare.call_count, 5)
        self.row.refresh_from_db(); self.assertEqual(self.row.otp_attempts, 5)

    def test_parallel_confirmation_claims_once(self):
        session = _claim_signer.sign(str(self.row.pk))
        statuses = self.parallel(2, lambda i: APIClient().post('/api/v1/claim/confirm/', {'claim_session': session}, format='json').status_code)
        self.assertEqual(sorted(statuses), [200, 400])
        from django_api.models import StudentProfile
        self.assertEqual(StudentProfile.objects.filter(email=self.row.email).count(), 1)
