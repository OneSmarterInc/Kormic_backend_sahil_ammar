from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.conf import settings
from django.utils import timezone
from django_api.models import AgentJob
from django_api.tests import make_student_client


class QueueDeadlineTests(TestCase):
    def setUp(self):
        self.client, self.sid = make_student_client()

    def test_status_expires_job_without_a_worker_or_scheduler(self):
        job = AgentJob.objects.create(owner_key='student:' + self.sid, student_id=self.sid,
            kind='student', idempotency_key='stopped-worker')
        AgentJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(seconds=settings.AGENT_QUEUE_TIMEOUT + 1))
        response = self.client.get(f'/api/chat/jobs/{job.pk}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'failed')
        job.refresh_from_db()
        self.assertIsNotNone(job.completed_at)

    def test_queued_job_exposes_real_state_and_deadline(self):
        job = AgentJob.objects.create(owner_key='student:' + self.sid, student_id=self.sid,
            kind='student', idempotency_key='queued')
        response = self.client.get(f'/api/chat/jobs/{job.pk}/')
        self.assertEqual(response.data['status'], 'queued')
        self.assertIn('expires_at', response.data)
        activity = self.client.get('/api/chat/activity/')
        self.assertEqual(activity.data['status'], 'queued')

    @override_settings(AGENT_QUEUE_ENABLED=True, AGENT_QUEUE_BACKEND='database')
    def test_new_submission_can_replace_an_expired_job_without_polling_first(self):
        old = AgentJob.objects.create(owner_key='student:' + self.sid, student_id=self.sid,
            kind='student', idempotency_key='old')
        AgentJob.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(seconds=settings.AGENT_QUEUE_TIMEOUT + 1))
        response = self.client.post('/api/chat/agent/', {'message':'Try again'}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        old.refresh_from_db()
        self.assertEqual(old.status, 'failed')
        self.assertNotEqual(response.data['job_id'], str(old.pk))
