from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from django.test import TestCase, override_settings
from django_api.models import AgentJob, ChatMessage
from pure_multi_agent.jobs import ready_jobs
from pure_multi_agent.tasks import execute_agent_job


@override_settings(AGENT_QUEUE_BACKEND='database', AGENT_DISTRIBUTED_LIMITS=True, AGENT_CAPACITY_BACKEND='database')
class DocumentQueueTests(TestCase):
    @override_settings(AGENT_QUEUE_BACKEND='celery')
    def test_celery_document_resume_stays_on_document_queue(self):
        from pure_multi_agent.capacity import ResumeTurnLater
        job = AgentJob.objects.create(owner_key='student:a:documents', student_id='a', kind='resume', idempotency_key='celery-busy')
        with patch('pure_multi_agent.document_jobs.run_document', side_effect=ResumeTurnLater({}, delay=5)), patch.object(execute_agent_job, 'apply_async') as dispatch:
            execute_agent_job.run(str(job.pk))
        self.assertEqual(dispatch.call_args.kwargs['queue'], 'agent_documents')
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')

    def test_linkedin_busy_extraction_reschedules_original_staged_file(self):
        import tempfile
        from github_profiles.scheduling import CapacityBusy
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / 'linkedin.txt'
            staged.write_text('City College')
            job = AgentJob.objects.create(owner_key='student:a:documents',student_id='a',kind='linkedin',
                idempotency_key='capacity',payload={'files':[{'path':'queued-documents/linkedin.txt','name':'linkedin.txt','content_type':'text/plain'}]})
            with patch('django_api.services.resolve_upload_path', return_value=staged), \
                 patch('django_api.services.analyze_linkedin', side_effect=CapacityBusy()):
                execute_agent_job.run(str(job.pk))
            job.refresh_from_db()
            self.assertEqual(job.status, 'queued')
            self.assertTrue(staged.exists())
            self.assertIsNotNone(job.dispatched_at)

    def test_linkedin_capacity_wait_preserves_queued_upload(self):
        from pure_multi_agent.capacity import ResumeTurnLater
        job = AgentJob.objects.create(owner_key='student:a:documents',student_id='a',kind='linkedin',idempotency_key='busy')
        with patch('pure_multi_agent.document_jobs.run_document', side_effect=ResumeTurnLater({}, delay=5)), \
             patch('pure_multi_agent.document_jobs.cleanup_staging') as cleanup:
            execute_agent_job.run(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.error, '')
        self.assertIsNone(job.completed_at)
        cleanup.assert_not_called()

    def test_retried_upload_admission_saves_one_job_and_one_file(self):
        from pure_multi_agent.document_jobs import submit_document
        request = SimpleNamespace(user=SimpleNamespace(account=SimpleNamespace(student_uuid='a')),
            headers={'Idempotency-Key':'upload-once'}, build_absolute_uri=lambda path:'https://example.test/')
        uploaded = SimpleNamespace(name='resume.pdf', content_type='application/pdf')
        with patch('django_api.services.validate_upload'), patch('django_api.services.save_uploaded_file',return_value=Path('staged.pdf')) as save, \
             patch('django_api.services.relative_upload_path',return_value='queued-documents/staged.pdf'):
            first = submit_document(request,'resume',[uploaded])
            again = submit_document(request,'resume',[uploaded])
        self.assertEqual(first.status_code,202)
        self.assertEqual(first.data['job_id'],again.data['job_id'])
        self.assertEqual(AgentJob.objects.count(),1)
        save.assert_called_once()

    def test_document_jobs_use_separate_workers(self):
        document = AgentJob.objects.create(owner_key='student:a:documents',student_id='a',kind='resume',idempotency_key='upload')
        chat = AgentJob.objects.create(owner_key='student:b',student_id='b',kind='student',idempotency_key='chat')
        self.assertEqual(ready_jobs(10,queue='chat'), [chat.pk])
        self.assertEqual(ready_jobs(10,queue='documents'), [document.pk])

    def test_document_delivery_is_idempotent_without_chat_message(self):
        job = AgentJob.objects.create(owner_key='student:a:documents',student_id='a',kind='resume',idempotency_key='upload')
        with patch('pure_multi_agent.document_jobs.run_document',return_value=({'resume_id':123,'status':'success'},None,{})) as extraction:
            execute_agent_job.run(str(job.pk)); execute_agent_job.run(str(job.pk))
        extraction.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status,'completed')
        self.assertEqual(job.result['resume_id'],123)
        self.assertEqual(ChatMessage.objects.count(),0)

    def test_safe_progress_is_fenced_and_does_not_expose_results(self):
        import uuid
        from pure_multi_agent.job_recovery import execution
        from pure_multi_agent.document_progress import report
        from pure_multi_agent.jobs import serialize
        token = uuid.uuid4()
        job = AgentJob.objects.create(owner_key='student:a:documents',kind='resume',status='processing',execution_token=token,idempotency_key='progress')
        marker = execution.set((job.pk, token))
        try:
            report('extracting')
        finally:
            execution.reset(marker)
        job.refresh_from_db()
        self.assertEqual(serialize(job)['progress']['stage'], 'extracting')
        self.assertNotIn('result', serialize(job))
        marker = execution.set((job.pk, uuid.uuid4()))
        try:
            report('saving')
        finally:
            execution.reset(marker)
        job.refresh_from_db()
        self.assertEqual(serialize(job)['progress']['stage'], 'extracting')

    def test_document_completion_notifies_owner_once(self):
        from accounts.models import Account
        from django.contrib.auth.models import User
        from django_api.models import StudentProfile
        from notifications.models import NotificationLog, PushDelivery
        student = StudentProfile.objects.create(name='Test student')
        account = Account.objects.create(user=User.objects.create_user('document-owner'), role='student', student_profile=student)
        job = AgentJob.objects.create(owner_key=f'student:{student.uuid}:documents',student_id=str(student.uuid),kind='linkedin',idempotency_key='notify')
        with patch('pure_multi_agent.document_jobs.run_document',return_value=({'status':'success'},None,{})):
            execute_agent_job.run(str(job.pk))
            execute_agent_job.run(str(job.pk))
        self.assertEqual(NotificationLog.objects.filter(account=account,event_type='profile_processed').count(),1)
        self.assertEqual(PushDelivery.objects.count(),1)

    def test_cv_skill_merge_preserves_github_skills(self):
        from django_api.profile_sources import merge_skills
        self.assertEqual(merge_skills(['Python','SQL'], ['python','React']), ['Python','SQL','React'])
