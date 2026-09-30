import uuid
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django_api.models import AgentJob, InferenceWaiter, InferenceTenant
from pure_multi_agent.job_recovery import recover, boundary, execution, ExecutionLost
from pure_multi_agent.inference_admission import choose_waiter


class RecoveryTests(TestCase):
    def job(self, phase, key='a'):
        return AgentJob.objects.create(owner_key=key, idempotency_key=key, kind='student', status='processing',
            execution_token=uuid.uuid4(), started_at=timezone.now()-timedelta(seconds=1000),
            heartbeat_at=timezone.now()-timedelta(seconds=100), recovery_phase=phase,
            payload={'resume_state':{'turn_id':'stable-turn'}})

    def test_university_evidence_checkpoint_preserves_database_values(self):
        from datetime import date
        from decimal import Decimal
        job = self.job('model')
        now = timezone.now()
        evidence = {'facts':[{'fetched_at':now,'source_quote':'Official evidence'}],
            'courses':[{'fee':Decimal('123.45'),'id':uuid.uuid4(),'start':date(2026,9,29)}]}
        ctx = {'university_answer_evidence':evidence, 'university_source_search_required':True}
        token = execution.set((job.pk, job.execution_token))
        try:
            boundary(ctx, tuple(ctx), phase='model')
        finally:
            execution.reset(token)
        job.refresh_from_db()
        saved = job.payload['resume_state']
        self.assertTrue(saved['university_source_search_required'])
        self.assertEqual(saved['university_answer_evidence']['courses'][0]['fee'],'123.45')
        self.assertEqual(saved['university_answer_evidence']['courses'][0]['start'],'2026-09-29')
        self.assertEqual(saved['university_answer_evidence']['facts'][0]['source_quote'],'Official evidence')
        self.assertIs(evidence['facts'][0]['fetched_at'],now)
        self.assertEqual(job.recovery_phase,'model')

    def test_only_safe_model_checkpoint_is_requeued(self):
        safe, unsafe = self.job('model'), self.job('unsafe','b')
        recover()
        safe.refresh_from_db(); unsafe.refresh_from_db()
        self.assertEqual(safe.status, 'queued')
        self.assertEqual(safe.recovery_attempts, 1)
        self.assertIsNone(safe.execution_token)
        self.assertEqual(unsafe.status, 'failed')
        self.assertEqual(safe.payload['resume_state']['turn_id'], 'stable-turn')

    def test_late_worker_cannot_cross_tool_boundary(self):
        job = self.job('model')
        token = execution.set((job.pk, job.execution_token))
        try:
            recover()
            with self.assertRaises(ExecutionLost): boundary()
        finally:
            execution.reset(token)

    def test_live_heartbeat_prevents_recovery(self):
        job = self.job('model')
        AgentJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now())
        recover(); job.refresh_from_db()
        self.assertEqual(job.status, 'processing')

    def test_tenant_with_many_conversations_cannot_jump_other_tenant(self):
        now = timezone.now()
        for index in range(10):
            InferenceWaiter.objects.create(provider='qwen', tenant='university:a', owner=f'university:a:student:{index}',
                priority=0, rank_at=now, expires_at=now+timedelta(seconds=40))
        other = InferenceWaiter.objects.create(provider='qwen', tenant='student:b', owner='student:b',
            priority=0, rank_at=now+timedelta(seconds=1), expires_at=now+timedelta(seconds=40))
        InferenceTenant.objects.create(provider='qwen', tenant='university:a', last_served_at=now)
        self.assertEqual(choose_waiter('qwen',now).pk, other.pk)

    def test_workflow_queue_reserves_room_for_other_tenants(self):
        from pure_multi_agent.jobs import ready_jobs
        for index in range(8):
            AgentJob.objects.create(owner_key=f'university:a:student:{index}', idempotency_key=str(index), kind='university')
        other = AgentJob.objects.create(owner_key='student:b', idempotency_key='other', kind='student')
        chosen = ready_jobs(10)
        self.assertIn(other.pk, chosen)
        self.assertEqual(len(chosen), 3)

    def test_busy_background_slot_does_not_block_chat(self):
        from agent_queries.models import AgentCapacitySlot
        now = timezone.now()
        AgentCapacitySlot.objects.create(key='inference-background:qwen', number=0, token=uuid.uuid4(), expires_at=now+timedelta(seconds=90))
        InferenceWaiter.objects.create(provider='qwen', tenant='background:a', owner='background:a', priority=20,
            rank_at=now-timedelta(seconds=35), expires_at=now+timedelta(seconds=40))
        chat = InferenceWaiter.objects.create(provider='qwen', tenant='student:a', owner='student:a', priority=0,
            rank_at=now, expires_at=now+timedelta(seconds=40))
        self.assertEqual(choose_waiter('qwen',now).pk, chat.pk)
