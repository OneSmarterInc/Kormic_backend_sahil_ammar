import io
import json
import tempfile
import zipfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.core.cache import cache
from django.utils import timezone
from django.contrib.auth.models import User
from django_api.test_chat_jobs import fixture
from django_api import models as m
from accounts.privacy import queue_deletion, safe_path
from accounts.privacy_tasks import process_deletions, apply_retention
from accounts.models import Account, TOTPDevice
from rest_framework.test import APIClient

class PrivacyTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user, self.profile, self.client=fixture()
        self.user.set_password("S3curePassw0rd!"); self.user.save()
        self.other, self.other_profile, _=fixture("other")
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.override=override_settings(MEDIA_ROOT=self.temp.name); self.override.enable(); self.addCleanup(self.override.disable)
    def test_export_requires_password_and_contains_only_own_data(self):
        self.assertEqual(self.client.post('/api/auth/privacy/export/', {'password':'wrong'}).status_code,403)
        own=m.ChatMessage.objects.create(student_id=str(self.profile.uuid),sender="user",content="My personal question")
        m.ChatMessage.objects.create(student_id=str(self.other_profile.uuid),sender="user",content="Other private question")
        file=Path(self.temp.name)/'resume.pdf'; file.write_bytes(b'my-resume')
        m.ResumeUpload.objects.create(student=self.profile,file_path=str(file),original_filename="resume.pdf")
        with patch('pure_multi_agent.runtime._checkpointer.get_tuple', return_value=None):
            response=self.client.post('/api/auth/privacy/export/', {'password':'S3curePassw0rd!'})
        self.assertEqual(response.status_code,200)
        archive=zipfile.ZipFile(io.BytesIO(b''.join(response.streaming_content))); data=json.loads(archive.read('data.json'))
        self.assertEqual([r['id'] for r in data['messages']],[own.pk]); self.assertNotIn('Other private',json.dumps(data)); self.assertNotIn('password',data['account'])
        self.assertEqual(archive.read(data['files'][0]['archive_path']),b'my-resume')
        response.close()
    def test_deletion_disables_immediately_and_retries_external_failure(self):
        response=self.client.post('/api/auth/privacy/delete/', {'password':'S3curePassw0rd!', 'confirmation':'DELETE'})
        self.assertEqual(response.status_code,202); self.user.refresh_from_db(); self.assertFalse(self.user.is_active)
        job=m.StudentDeletion.objects.get(pk=response.data['deletion_id']); job.not_before=timezone.now(); job.save()
        with patch('accounts.privacy_tasks.revoke_github', side_effect=RuntimeError('unavailable')): process_deletions()
        job.refresh_from_db(); self.assertEqual(job.status,'retry_pending'); self.assertIsNone(job.completed_at); self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        job.not_before=timezone.now(); job.save()
        m.AriaMemory.objects.create(student_id=str(self.profile.uuid))
        with patch('accounts.privacy_tasks.revoke_github'), patch('pure_multi_agent.runtime.reset_conversation') as reset:
            process_deletions(); reset.assert_called_once_with(str(self.profile.uuid))
        job.refresh_from_db(); self.assertEqual(job.status,'completed'); self.assertIsNone(job.user_id); self.assertFalse(m.StudentProfile.objects.filter(pk=self.profile.pk).exists()); self.assertTrue(User.objects.filter(pk=self.other.pk).exists())
        self.assertFalse(m.AriaMemory.objects.filter(student_id=str(self.profile.uuid)).exists())
    def test_path_boundary_rejects_parent_and_symlink(self):
        with self.assertRaises(ValueError): safe_path('/etc/passwd')
        link=Path(self.temp.name)/'outside'; link.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError): safe_path(str(link))
    def test_retention_erases_expired_transcript_and_checkpoint_for_active_account(self):
        message=m.ChatMessage.objects.create(student_id=str(self.profile.uuid),sender='user',content='expired')
        m.ChatMessage.objects.filter(pk=message.pk).update(created_at=timezone.now()-timedelta(days=181))
        with patch('pure_multi_agent.runtime.reset_conversation') as reset:
            apply_retention(); reset.assert_any_call(str(self.profile.uuid))
        self.assertFalse(m.ChatMessage.objects.filter(pk=message.pk).exists()); self.user.refresh_from_db(); self.assertTrue(self.user.is_active)
    def test_student_cannot_set_institution_policy(self):
        self.assertEqual(self.client.patch('/api/auth/privacy/retention/', {'roster_days':1},format='json').status_code,403)
    def test_university_scope_cannot_extend_or_change_other_policy(self):
        from universities.models import University
        university=University.objects.create(name='Own')
        account=self.user.account; account.role='university'; account.university=university; account.save()
        self.assertEqual(self.client.patch('/api/auth/privacy/retention/', {'transcript_days':181},format='json').status_code,400)
        self.assertEqual(self.client.patch('/api/auth/privacy/retention/', {'roster_days':1},format='json').status_code,400)
        response=self.client.patch('/api/auth/privacy/retention/', {'transcript_days':30},format='json')
        self.assertEqual(response.status_code,200); self.assertEqual(response.data['scope'],f'university:{university.uuid}')

    def test_memory_expires_even_if_profile_is_active_and_transcripts_were_cleared(self):
        m.StudentProfile.objects.filter(pk=self.profile.pk).update(memory_reset_at=timezone.now()-timedelta(days=181))
        m.AriaMemory.objects.create(student_id=str(self.profile.uuid), important_points=["old private point"])
        with patch('pure_multi_agent.runtime.reset_conversation') as reset:
            apply_retention(); reset.assert_any_call(str(self.profile.uuid))
        self.assertFalse(m.AriaMemory.objects.filter(student_id=str(self.profile.uuid)).exists())
        self.profile.refresh_from_db(); self.assertGreater(self.profile.memory_reset_at,timezone.now()-timedelta(minutes=1))
