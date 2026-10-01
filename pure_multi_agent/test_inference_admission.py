from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from django_api.models import InferenceWaiter, AgentJob, ChatMessage
from pure_multi_agent.inference_admission import admission, chat_workload
from pure_multi_agent.tasks import execute_agent_job


class AdmissionTests(TestCase):
    def test_presenter_conversations_have_separate_locks(self):
        from pure_multi_agent.jobs import owner_key
        self.assertNotEqual(owner_key(None, 'uni', 'student1'), owner_key(None, 'uni', 'student2'))
        self.assertNotEqual(owner_key(None, 'uni'), owner_key(None, 'uni', 'student1'))

    @override_settings(AGENT_QUEUE_BACKEND='database', AGENT_DISTRIBUTED_LIMITS=True, AGENT_CAPACITY_BACKEND='database')
    def test_capacity_yield_persists_checkpoint_without_reply(self):
        from pure_multi_agent.capacity import ResumeTurnLater
        job = AgentJob.objects.create(owner_key='student:pause', idempotency_key='pause', kind='student')
        with patch('pure_multi_agent.jobs.run', side_effect=ResumeTurnLater({'step': 2}, 5)):
            execute_agent_job.run(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.payload['resume_state'], {'step':2})
        self.assertGreater(job.dispatched_at, timezone.now())
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_ticket_removed_and_slot_released_when_model_raises(self):
        events = []
        @contextmanager
        def acquire(*args):
            events.append('acquire')
            try:
                yield
            finally:
                events.append('release')
        with self.assertRaises(ValueError), chat_workload('student:test'):
            with admission('qwen', None, 100, acquire):
                self.assertEqual(InferenceWaiter.objects.count(), 0)
                raise ValueError('provider failed')
        self.assertEqual(events, ['acquire', 'release'])
        self.assertEqual(InferenceWaiter.objects.count(), 0)

    def test_older_background_ticket_precedes_new_chat(self):
        ticket = InferenceWaiter.objects.create(provider='qwen', owner='background',
            rank_at=timezone.now()-timedelta(seconds=6), expires_at=timezone.now()+timedelta(seconds=10))
        events = []
        @contextmanager
        def acquire(*args):
            events.append('chat')
            yield
        def finish_background(*args):
            self.assertEqual(events, [])
            events.append('background')
            ticket.delete()
        with patch('pure_multi_agent.inference_admission.time.sleep', side_effect=finish_background), chat_workload('student:test'):
            with admission('qwen', None, 100, acquire):
                pass
        self.assertEqual(events, ['background', 'chat'])

    @override_settings(AGENT_QUEUE_BACKEND='database', AGENT_DISTRIBUTED_LIMITS=True, AGENT_CAPACITY_BACKEND='database')
    def test_duplicate_delivery_commits_one_reply(self):
        job = AgentJob.objects.create(owner_key='student:test', idempotency_key='test', kind='student')
        with patch('pure_multi_agent.jobs.run', return_value=({'reply': 'Answer'}, {'channel':'agent', 'student_id':'test'}, {})) as run:
            execute_agent_job.run(str(job.pk))
            execute_agent_job.run(str(job.pk))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(ChatMessage.objects.filter(student_id='test', sender='assistant').count(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
