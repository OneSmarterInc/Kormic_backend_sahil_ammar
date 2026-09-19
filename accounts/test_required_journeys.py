"""Section 11 API journeys: real auth, ORM, claims and session restoration.

Only outbound OTP delivery is replaced; the generated OTP, signature validation,
canonical identity and provenance all use production code.
"""
from unittest.mock import patch
import pyotp
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from accounts.models import Account
from django_api.models import StudentProfile
from institutes.services import register_institute
from institutes_list.models import ListedStudent, UniversityStudentList
from institutes_list.tasks import claim_otp_cache_key
from notifications.models import NotificationLog, PushToken


class RequiredStudentJourneys(TestCase):
    password = 'Journey-Secure-2026!'
    email = 'journey@example.com'

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.delivery = patch('institutes_list.claim_views.send_claim_otp_email_task.delay').start()
        self.addCleanup(patch.stopall)

    def post(self, path, data, code=200, client=None):
        response = (client or self.client).post('/api/v1/' + path, data, format='json')
        self.assertEqual(response.status_code, code, response.data)
        return response.data

    def register(self):
        data = self.post('auth/register/', {'email': self.email, 'password': self.password,
                         'name': 'Student Name', 'role': 'student'}, 201)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + data['access'])
        secret = self.post('auth/totp/enroll/', {})['secret']
        self.post('auth/totp/verify-enrollment/', {'code': pyotp.TOTP(secret).now()})
        self.client.credentials()
        challenge = self.post('auth/login/', {'email': self.email, 'password': self.password})
        tokens = self.post('auth/verify-totp/', {'mfa_token': challenge['mfa_token'], 'code': pyotp.TOTP(secret).now()})
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])
        return tokens, secret

    def invitation(self):
        institute = register_institute('Journey Institute')
        roster = UniversityStudentList.objects.create(institute=institute, contact_name='Registrar',
                                                      contact_email='registrar@example.com')
        return ListedStudent.objects.create(source_list=roster, institute_id=str(institute.uuid),
                    email=self.email, full_name='Institute Name', field_of_study='Physics',
                    degree_level='Bachelor', expected_graduation='05/2028')

    def claim(self, row):
        # New anonymous client reproduces following a link, without reusing login state.
        anonymous = APIClient()
        start = self.post('claim/start/', {'token': row.claim_token}, client=anonymous)
        self.assertEqual(set(start), {'masked_email'})
        row.refresh_from_db()
        otp = cache.get(claim_otp_cache_key(row.pk, row.otp_hash))
        self.assertTrue(otp)
        verified = self.post('claim/verify/', {'token': row.claim_token, 'code': otp}, client=anonymous)
        self.assertEqual(verified['prefill']['field_of_study'], 'Physics')
        payload = {'claim_session': verified['claim_session'], 'fields': {'field_of_study': 'Mathematics'}}
        result = self.post('claim/confirm/', payload, client=anonymous)
        self.post('claim/confirm/', payload, code=400, client=anonymous)
        self.post('claim/start/', {'token': row.claim_token}, code=404, client=anonymous)
        return result

    def assert_canonical(self, row, expected_id):
        account = Account.objects.get(user__email=self.email)
        self.assertEqual(StudentProfile.objects.filter(email__iexact=self.email).count(), 1)
        self.assertEqual(str(account.student_profile.uuid), expected_id)
        row.refresh_from_db()
        self.assertEqual(row.claimed_student_id, expected_id)
        self.assertEqual(row.divergences[0]['list_value'], 'Physics')
        self.assertEqual(row.divergences[0]['student_value'], 'Mathematics')
        self.assertEqual(account.student_profile.extra_data['institute_sourced']['list_id'], row.source_list_id)
        return account

    def restore(self, tokens):
        fresh = APIClient()
        refreshed = self.post('auth/refresh/', {'refresh': tokens['refresh']}, client=fresh)
        fresh.credentials(HTTP_AUTHORIZATION='Bearer ' + refreshed['access'])
        me = fresh.get('/api/v1/auth/me/')
        self.assertEqual(me.status_code, 200, me.data)
        return fresh, me.data, refreshed

    def test_claim_then_registration_reload_preserves_single_identity_and_correction(self):
        row = self.invitation()
        claimed = self.claim(row)
        tokens, _ = self.register()
        self.assert_canonical(row, claimed['student_id'])
        fresh, me, _ = self.restore(tokens)
        self.assertEqual(me['student_id'], claimed['student_id'])
        profile = fresh.get('/api/v1/profile/' + claimed['student_id'] + '/')
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(StudentProfile.objects.get(email=self.email).major, 'Mathematics')

    def test_registration_then_claim_keeps_student_values_and_records_correction(self):
        tokens, _ = self.register()
        account = Account.objects.get(user__email=self.email)
        profile = account.student_profile
        profile.major = 'Computer Science'
        profile.save()
        row = self.invitation()
        claimed = self.claim(row)
        self.assertEqual(claimed['student_id'], str(profile.uuid))
        self.assert_canonical(row, str(profile.uuid))
        _, me, _ = self.restore(tokens)
        self.assertEqual(me['student_id'], str(profile.uuid))
        profile.refresh_from_db()
        self.assertEqual(profile.major, 'Computer Science')

    def test_basic_info_reload_skip_preferences_notification_and_logout(self):
        tokens, _ = self.register()
        payload = {'name': 'Student Name', 'email': self.email, 'phone': '+12025550123',
                   'country': 'United States', 'date_of_birth': '02/03/2004', 'city': 'Dayton',
                   'region': 'Ohio', 'institution': 'Journey College', 'major': 'Physics',
                   'program': 'Bachelor', 'year_in_college': '3rd Year', 'graduation_year': 2028,
                   'interests': ['Study abroad'], 'target_degree_or_field': 'MS Physics'}
        response = self.client.post('/api/v1/profile/', payload, format='json')
        self.assertIn(response.status_code, (200, 201), response.data)
        response = self.client.patch('/api/v1/auth/onboarding/preferences/',
                    {'github_onboarding_state': 'skipped', 'linkedin_onboarding_state': 'skipped'}, format='json')
        self.assertEqual(response.status_code, 200)
        fresh, me, refreshed = self.restore(tokens)
        self.assertTrue(me['onboarding']['basic_info_complete'])
        self.assertEqual(me['onboarding']['github_onboarding_state'], 'skipped')
        self.assertFalse(me['onboarding']['github_connected'])
        account = Account.objects.get(user__email=self.email)
        self.assertEqual(account.student_profile.evidence['manual_profile_api']['city'], 'Dayton')
        token = 'ExponentPushToken[journey-device]'
        self.post('notifications/register-token/', {'token': token, 'platform': 'android'}, client=fresh)
        NotificationLog.objects.create(account=account, event_type='agent_reply', title='Aria',
                    body='Your answer', data={'student_id': me['student_id'], 'screen': 'BotScreen'})
        polled = fresh.get('/api/v1/notifications/poll/')
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.data['results'][0]['data']['student_id'], me['student_id'])
        self.post('notifications/unregister-token/', {'token': token}, client=fresh)
        self.assertFalse(PushToken.objects.get(token=token).is_active)
        self.post('auth/logout/', {'refresh': refreshed.get('refresh', tokens['refresh'])}, code=205, client=fresh)
        rejected = APIClient().post('/api/v1/auth/refresh/',
                    {'refresh': refreshed.get('refresh', tokens['refresh'])}, format='json')
        self.assertEqual(rejected.status_code, 401)
