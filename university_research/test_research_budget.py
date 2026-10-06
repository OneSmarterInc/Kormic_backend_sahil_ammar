from uuid import uuid4
from django.test import TestCase
from django_api.models import AgentJob
from pure_multi_agent.job_recovery import execution
from university_research.research_budget import claim, cached, complete


class ResearchBudgetTests(TestCase):
    def test_raw_response_survives_processing_failure_and_replays_without_provider(self):
        import json
        from unittest.mock import patch
        from university_research.research_budget import preserve_response,raw_response
        from university_research.new_university import research
        job=AgentJob.objects.create(owner_key='raw',idempotency_key='raw',kind='student',status='processing',execution_token=uuid4())
        marker=execution.set((job.pk,job.execution_token))
        raw=json.dumps({'name':'Example','official_website':'https://example.edu/', 'answer':'Original answer',
            'catalogue':{'courses':[{'name':'Physics','tuition':64000,'seats':None}]}})
        try:
            claim({},'example.edu')
            preserve_response({},'example.edu',raw,'claude-test','fees')
            with patch('pure_multi_agent.model_router.invoke') as client:
                page=research({'new_university_domains':['example.edu']},'https://example.edu/','fees')
            client.assert_not_called()
            self.assertEqual(raw_response({},'example.edu')['raw'],raw)
            self.assertEqual(page['evidence_provider'],'claude_research')
            self.assertEqual(page['catalogue']['courses'][0]['tuition'],'64000')
        finally:
            execution.reset(marker)

    def test_after_reply_does_not_queue_scrape_after_fallback_attempt(self):
        from unittest.mock import patch
        from university_research.models import PublicUniversity
        from university_research.services import publish_delivered_evidence
        row=PublicUniversity.objects.create(identity_key='no-duplicate',name='Example',website='https://example.edu/')
        job=AgentJob.objects.create(owner_key='duplicate',idempotency_key='duplicate',kind='student',status='completed',payload={
            'claude_research':{'attempted':True,'domain':'example.edu'},
            'university_cache_pending':{'research':[str(row.pk)]}})
        with patch('university_research.services.queue_research') as queue:
            publish_delivered_evidence(job.pk)
        queue.assert_not_called()
        job.refresh_from_db()
        self.assertTrue(job.payload['claude_research']['attempted'])

    def test_budget_survives_recovery_and_is_isolated_between_jobs(self):
        job = AgentJob.objects.create(owner_key='one', idempotency_key='turn', kind='student', status='processing', execution_token=uuid4())
        token = execution.set((job.pk, job.execution_token))
        try:
            claim({}, 'example.edu')
            with self.assertRaises(ValueError):
                claim({}, 'example.edu')
            complete({}, 'example.edu', {'answer':'Saved result'})
            self.assertEqual(cached({}, 'example.edu'), {'answer':'Saved result'})
            other = AgentJob.objects.create(owner_key='two', idempotency_key='turn', kind='student', status='processing', execution_token=uuid4())
            execution.set((other.pk, other.execution_token))
            claim({}, 'example.edu')
        finally:
            execution.reset(token)


    def test_capacity_pause_keeps_budget_written_during_turn(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from django.test import override_settings
        from pure_multi_agent.tasks import execute_agent_job
        from pure_multi_agent.capacity import ResumeTurnLater
        job = AgentJob.objects.create(owner_key='pause', idempotency_key='turn', kind='student')
        def run(stale_job):
            claim({}, 'example.edu')
            raise ResumeTurnLater({'model_steps':3})
        with override_settings(AGENT_QUEUE_BACKEND='database'), \
             patch('pure_multi_agent.job_recovery.track', side_effect=lambda pk, token: tracking(pk, token)), \
             patch('pure_multi_agent.job_recovery.recover'), \
             patch('pure_multi_agent.capacity.lease', return_value=nullcontext()), \
             patch('pure_multi_agent.jobs.run', side_effect=run):
            execute_agent_job.run(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')
        self.assertTrue(job.payload['claude_research']['attempted'])
        self.assertEqual(job.payload['resume_state'], {'model_steps':3})


from contextlib import contextmanager
@contextmanager
def tracking(pk, token):
    marker = execution.set((pk, token))
    try:
        yield
    finally:
        execution.reset(marker)
