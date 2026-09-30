from django.test import TestCase
from django_api.models import AgentJob
from university_research.models import PublicUniversity
from university_research.services import publish_delivered_evidence

class DeliveredCacheTests(TestCase):
    def test_direct_claude_answer_is_cached_after_delivery_not_as_scraped_facts(self):
        row=PublicUniversity.objects.create(identity_key='direct',name='Example University',website='https://example.edu/')
        answer={'text':'Courses, fees, seats and scholarships: N/A','sources':[row.website],'provider':'claude_direct'}
        page={'url':row.website,'title':row.name,'content':answer['text'],'provider_answer':answer,'evidence_provider':'claude_direct'}
        job=AgentJob.objects.create(owner_key='student:direct',idempotency_key='direct',kind='student',status='processing',
            payload={'university_cache_pending':{'pages':[{'university_id':str(row.pk),'page':page}]}})
        self.assertFalse(publish_delivered_evidence(job.pk))
        row.refresh_from_db()
        self.assertNotIn('provider_answer',row.coverage or {})
        job.status='completed'; job.save(update_fields=['status'])
        self.assertTrue(publish_delivered_evidence(job.pk))
        row.refresh_from_db()
        self.assertEqual(row.coverage['provider_answer'],answer)
        self.assertIsNotNone(row.fetched_at)
        self.assertFalse(row.pages.exists())
        self.assertFalse(row.facts.exists())

    def test_requires_completed_reply_and_is_idempotent(self):
        row=PublicUniversity.objects.create(identity_key='example',name='Example',website='https://example.edu/')
        answer={'text':'Courses: MSc Physics. Scholarships: apply online.','sources':['https://example.edu/courses'],'topics':'courses scholarships','retrieved_at':'2026-09-29T12:00:00+00:00'}
        page={'url':'https://example.edu/courses','title':'Courses','content':'MSc Physics courses are offered. Fees are not published.','provider_answer':answer}
        pending={'pages':[{'university_id':str(row.pk),'page':page}], 'universities':[str(row.pk)],'missing':['fees'],'checked':['fees']}
        job=AgentJob.objects.create(owner_key='student:test',idempotency_key='test',kind='student',status='processing',payload={'university_cache_pending':pending})
        self.assertFalse(publish_delivered_evidence(job.pk))
        self.assertFalse(row.pages.exists())
        job.status='completed';job.save(update_fields=['status'])
        self.assertFalse(row.pages.exists())
        self.assertTrue(publish_delivered_evidence(job.pk))
        row.refresh_from_db()
        self.assertEqual(row.coverage['unavailable_fields']['fees'],'N/A')
        self.assertEqual(row.coverage['provider_answer'],answer)
        self.assertEqual(row.pages.count(),1)
        self.assertTrue(row.facts.exists())
        self.assertTrue(publish_delivered_evidence(job.pk))
        self.assertEqual(row.pages.count(),1)
        job.refresh_from_db()
        self.assertNotIn('university_cache_pending',job.payload)

    def test_delivery_ack_cannot_publish_another_students_cache(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from rest_framework.test import APIRequestFactory, force_authenticate
        from django_api.job_views import job_status
        job=AgentJob.objects.create(owner_key='student:other',student_id='other',idempotency_key='other',kind='student',status='completed')
        request=APIRequestFactory().post('/chat/jobs/'+str(job.pk)+'/',{})
        user=SimpleNamespace(is_authenticated=True,account=SimpleNamespace(role='student',student_uuid='mine'))
        force_authenticate(request,user=user)
        with patch('accounts.permissions.IsTOTPEnrolled.has_permission',return_value=True), patch('university_research.services.publish_delivered_evidence') as publish:
            response=job_status(request,job_id=job.pk)
        self.assertEqual(response.status_code,404)
        publish.assert_not_called()
