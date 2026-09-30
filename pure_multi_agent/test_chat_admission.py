from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from django_api.models import StudentProfile, AgentJob, ChatMessage


@override_settings(AGENT_QUEUE_ENABLED=True, AGENT_DISTRIBUTED_LIMITS=False, AGENT_QUEUE_BACKEND='database')
class ChatAdmissionLockTests(TestCase):
    def setUp(self):
        profile = StudentProfile.objects.create(name='Chat test')
        user = User.objects.create_user('chat-lock-test')
        Account.objects.create(user=user, role='student', student_profile=profile)
        TOTPDevice.objects.create(user=user, secret_encrypted='fixture', confirmed_at=timezone.now())
        self.client = APIClient()
        self.client.force_authenticate(user)

    def send(self):
        return self.client.post('/api/chat/agent/', {'message': 'Hello'}, format='json', HTTP_IDEMPOTENCY_KEY='lock-test')

    def test_lock_during_job_insert_retries_transaction_without_duplicate_message(self):
        create = AgentJob.objects.create
        attempts = []
        def insert(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OperationalError('database is locked')
            return create(**kwargs)
        with patch('pure_multi_agent.jobs.AgentJob.objects.create', side_effect=insert):
            response = self.send()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(ChatMessage.objects.count(), 1)
        self.assertEqual(AgentJob.objects.count(), 1)
        repeated = self.send()
        self.assertEqual(repeated.data['job_id'], response.data['job_id'])
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_persistent_lock_returns_json_and_rolls_back_message(self):
        with patch('pure_multi_agent.jobs.AgentJob.objects.create', side_effect=OperationalError('database is locked')):
            response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response['Content-Type'].startswith('application/json'))
        self.assertEqual(response['Retry-After'], '2')
        self.assertFalse(ChatMessage.objects.exists())
        self.assertFalse(AgentJob.objects.exists())
