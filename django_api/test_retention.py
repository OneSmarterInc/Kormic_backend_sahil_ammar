from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import GitHubOAuthConnection
from django_api.models import (
    AgentAuditLog, AgentJob, ChatMessage, DataRetentionHold, GitHubAgentCheckpoint,
    GitHubAgentWrite, GitHubProfileSnapshot, GitHubSyncRun, StudentProfile,
)
from django_api.retention import restore_test_is_recent, run_retention
from pure_multi_agent.jobs import serialize
from pure_multi_agent.tasks import apply_agent_retention
from django_api.tasks import cleanup_old_audit_logs_task


@override_settings(AGENT_RESULT_RETENTION_DAYS=90, GITHUB_CHECKPOINT_RETENTION_DAYS=30)
class RetentionTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name="Retention student")
        self.student_id = str(self.student.uuid)
        self.old = timezone.now() - timedelta(days=100)

    def chat_job(self, *, completed_at=None, status="completed", pending=False, associate=True):
        user = ChatMessage.objects.create(channel="agent", student_id=self.student_id,
                                          sender="user", content="Question")
        assistant = ChatMessage.objects.create(channel="agent", student_id=self.student_id,
                                               sender="assistant", content="Answer")
        payload = {"message_id": user.pk, "message": "Question", "actor_id": 1,
                   "resume_state": {"university_candidates": ["uni-1"], "tool_trace": "long"}}
        if associate:
            payload["assistant_message_id"] = assistant.pk
        return AgentJob.objects.create(
            owner_key=f"student:{self.student_id}", idempotency_key=str(user.pk),
            kind="student", student_id=self.student_id, status=status,
            completed_at=completed_at or self.old, payload=payload,
            result={"agent": "Aria", "student_id": self.student_id, "reply": "Answer",
                    "message_id": user.pk, "pending": pending, "query_id": None,
                    "meta": {"large_tool_state": "long"}},
        )

    def github_checkpoint(self, *, status="completed", updated_at=None):
        profile = GitHubProfileSnapshot.objects.filter(student=self.student).first()
        if profile is None:
            user = User.objects.create(username="gh-retention")
            connection = GitHubOAuthConnection.objects.create(
                user=user, github_user_id=user.pk + 100,
                github_username=user.username, access_token_encrypted="test",
            )
            profile = GitHubProfileSnapshot.objects.create(
                student=self.student, connection=connection, github_user_id=connection.github_user_id,
            )
        run = GitHubSyncRun.objects.create(profile=profile, status=status)
        GitHubSyncRun.objects.filter(pk=run.pk).update(updated_at=updated_at or self.old)
        checkpoint = GitHubAgentCheckpoint.objects.create(
            run=run, thread="repo:main", checkpoint_id=str(run.pk), payload_type="json", payload=b"old",
        )
        GitHubAgentWrite.objects.create(
            checkpoint=checkpoint, task_id="task", index=0, channel="messages",
            payload_type="json", payload=b"old-write",
        )
        return checkpoint

    def test_dry_run_then_apply_preserves_chat_and_idempotency(self):
        job = self.chat_job()
        checkpoint = self.github_checkpoint()
        self.assertEqual(run_retention(apply=False), {
            "jobs_compacted": 1, "jobs_skipped": 0, "github_checkpoints_deleted": 1,
        })
        job.refresh_from_db()
        self.assertIn("meta", job.result)
        self.assertTrue(GitHubAgentCheckpoint.objects.filter(pk=checkpoint.pk).exists())

        self.assertEqual(run_retention(apply=True), {
            "jobs_compacted": 1, "jobs_skipped": 0, "github_checkpoints_deleted": 1,
        })
        job.refresh_from_db()
        self.assertTrue(job.result["archived_to_history"])
        self.assertNotIn("meta", job.result)
        self.assertEqual(job.payload["resume_state"]["university_candidates"], ["uni-1"])
        self.assertNotIn("tool_trace", job.payload["resume_state"])
        self.assertEqual(serialize(job)["result"]["reply"], "Answer")
        self.assertEqual(ChatMessage.objects.filter(student_id=self.student_id).count(), 2)
        self.assertFalse(GitHubAgentCheckpoint.objects.filter(pk=checkpoint.pk).exists())
        self.assertFalse(GitHubAgentWrite.objects.exists())
        self.assertEqual(run_retention(apply=True), {
            "jobs_compacted": 0, "jobs_skipped": 0, "github_checkpoints_deleted": 0,
        })

    def test_only_old_terminal_verified_data_is_eligible(self):
        self.chat_job(completed_at=timezone.now())
        self.chat_job(status="queued")
        self.chat_job(pending=True)
        self.chat_job(associate=False)
        self.github_checkpoint(status="running")
        self.github_checkpoint(updated_at=timezone.now())
        self.assertEqual(run_retention(apply=True), {
            "jobs_compacted": 0, "jobs_skipped": 0, "github_checkpoints_deleted": 0,
        })

    def test_student_and_global_holds_block_both_classes(self):
        self.chat_job()
        self.github_checkpoint()
        hold = DataRetentionHold.objects.create(subject_key=f"student:{self.student_id}", reason="Review")
        self.assertEqual(run_retention(apply=True), {
            "jobs_compacted": 0, "jobs_skipped": 0, "github_checkpoints_deleted": 0,
        })
        hold.released_at = timezone.now()
        hold.save(update_fields=["released_at"])
        global_hold = DataRetentionHold.objects.create(subject_key="*", reason="Incident")
        self.assertEqual(run_retention(apply=True), {
            "jobs_compacted": 0, "jobs_skipped": 0, "github_checkpoints_deleted": 0,
        })
        global_hold.released_at = timezone.now()
        global_hold.save(update_fields=["released_at"])
        self.assertEqual(run_retention(apply=True)["jobs_compacted"], 1)

    def test_missing_visible_reply_is_skipped_without_blocking_next_batch(self):
        invalid = self.chat_job(completed_at=self.old - timedelta(days=1))
        ChatMessage.objects.filter(pk=invalid.payload["assistant_message_id"]).delete()
        valid = self.chat_job()
        self.assertEqual(run_retention(apply=True, max_rows=1)["jobs_skipped"], 1)
        invalid.refresh_from_db()
        self.assertIsNotNone(invalid.retention_checked_at)
        self.assertIsNone(invalid.retention_compacted_at)
        self.assertEqual(run_retention(apply=True, max_rows=1)["jobs_compacted"], 1)
        valid.refresh_from_db()
        self.assertIsNotNone(valid.retention_compacted_at)

    def test_command_and_scheduled_task_require_restore_attestation(self):
        self.chat_job()
        with self.assertRaises(CommandError):
            call_command("prune_agent_retention", "--apply", stdout=StringIO())
        with override_settings(AGENT_RETENTION_ENABLED=True, AGENT_RETENTION_POLICY_APPROVED=True,
                               AGENT_RETENTION_RESTORE_TESTED_BACKUP_ID=""):
            self.assertIn("skipped", apply_agent_retention())
        self.assertEqual(AgentJob.objects.filter(retention_compacted_at__isnull=False).count(), 0)
        call_command("prune_agent_retention", "--apply", "--policy-approved",
                     "--restore-tested-backup-id=staging-restore-123",
                     f"--restore-tested-at={timezone.localdate().isoformat()}", stdout=StringIO())
        self.assertEqual(AgentJob.objects.filter(retention_compacted_at__isnull=False).count(), 1)

    def test_restore_attestation_expires(self):
        today = timezone.localdate()
        self.assertTrue(restore_test_is_recent("backup-1", today.isoformat(), today=today))
        self.assertFalse(restore_test_is_recent("backup-1", (today - timedelta(days=31)).isoformat(), today=today))
        self.assertFalse(restore_test_is_recent("backup-1", (today + timedelta(days=1)).isoformat(), today=today))
        self.assertFalse(restore_test_is_recent("backup-1", "not-a-date", today=today))

    def test_nightly_task_runs_only_with_current_approval(self):
        self.chat_job()
        with override_settings(AGENT_RETENTION_ENABLED=True, AGENT_RETENTION_POLICY_APPROVED=True,
                               AGENT_RETENTION_RESTORE_TESTED_BACKUP_ID="backup-1",
                               AGENT_RETENTION_RESTORE_TESTED_AT=timezone.localdate().isoformat()):
            self.assertEqual(apply_agent_retention()["jobs_compacted"], 1)

    def test_legacy_audit_cleanup_no_longer_purges_logs(self):
        log = AgentAuditLog.objects.create(run_id="audit-1", student_id=self.student_id,
                                           actor_agent="Aria", action_type="TOOL_CALL")
        AgentAuditLog.objects.filter(pk=log.pk).update(timestamp=self.old)
        self.assertEqual(cleanup_old_audit_logs_task(), 0)
        self.assertTrue(AgentAuditLog.objects.filter(pk=log.pk).exists())
