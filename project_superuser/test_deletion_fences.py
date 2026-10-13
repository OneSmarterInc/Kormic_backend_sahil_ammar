from unittest.mock import patch

from django.test import TestCase
from django_api.models import AgentJob, AgentAuditLog, StudentProfile
from project_superuser.services import purge_student_data, StudentWorkInProgress
from pure_multi_agent.tasks import execute_agent_job


class StudentDeletionTests(TestCase):
    def setUp(self):
        self.profile = StudentProfile.objects.create(name='Deletion test')
        self.sid = str(self.profile.uuid)

    def job(self, status='queued'):
        return AgentJob.objects.create(owner_key='student:' + self.sid, student_id=self.sid,
            kind='student', idempotency_key='test', status=status, payload={'message': 'private text'},
            result={'reply': 'private answer'})

    @patch('pure_multi_agent.runtime.reset_conversation')
    def test_deletion_purges_private_jobs_and_fences_late_delivery(self, reset):
        job = self.job()
        AgentAuditLog.objects.create(student_id=self.sid, actor_agent='Student', action_type='RUN_START')
        purge_student_data(self.sid)
        reset.assert_called_once_with(self.sid)
        self.assertFalse(AgentJob.objects.filter(student_id=self.sid).exists())
        self.assertFalse(AgentAuditLog.objects.filter(student_id=self.sid).exists())
        with patch('pure_multi_agent.jobs.run') as run:
            execute_agent_job.run(str(job.pk))
        run.assert_not_called()
        self.assertFalse(StudentProfile.objects.filter(uuid=self.sid).exists())

    def test_running_work_prevents_partial_deletion(self):
        job = self.job('processing')
        with self.assertRaises(StudentWorkInProgress):
            purge_student_data(self.sid)
        self.assertTrue(StudentProfile.objects.filter(uuid=self.sid).exists())
        self.assertTrue(AgentJob.objects.filter(pk=job.pk).exists())

    def test_fenced_worker_cannot_recreate_deleted_profile(self):
        from uuid import uuid4
        from django_api.services import save_profile_data
        from pure_multi_agent.job_recovery import execution, ExecutionLost
        self.profile.delete()
        token = execution.set((uuid4(), uuid4()))
        try:
            with self.assertRaises(ExecutionLost):
                save_profile_data(self.sid, {'name': 'Late worker'})
        finally:
            execution.reset(token)
        self.assertFalse(StudentProfile.objects.filter(uuid=self.sid).exists())
