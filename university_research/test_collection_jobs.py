from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from university_research.collection_jobs import collect_once, research_scope, public_collection_query
from university_research.models import PublicCollectionJob, PublicUniversity


class PublicCollectionTests(TestCase):
    def setUp(self):
        self.row = PublicUniversity.objects.create(identity_key='collection', name='Example',
            website='https://example.edu/', country='US')
        self.context = {'study_focus': {'target_degree': 'MS', 'target_subject': 'Computer Science',
            'target_intake': 'Fall 2027'}, 'student_profile': {'country': 'IN', 'gpa': '9.1'}}

    def test_scope_merges_public_request_but_separates_programme_intake_category_and_topic(self):
        question = 'What are the IELTS requirements?'
        first = research_scope(self.row, question, self.context)
        self.assertEqual(first, research_scope(self.row, question, {
            **self.context, 'student_profile': {'country': 'IN', 'gpa': '7.2'}}))
        self.assertNotIn('9.1', str(first))
        self.assertNotEqual(first, research_scope(self.row, question, {
            **self.context, 'study_focus': {**self.context['study_focus'], 'target_subject': 'Data Science'}}))
        self.assertNotEqual(first, research_scope(self.row, question, {
            **self.context, 'study_focus': {**self.context['study_focus'], 'target_intake': 'Spring 2028'}}))
        self.assertNotEqual(first, research_scope(self.row, question, {
            **self.context, 'student_profile': {'country': 'US'}}))
        self.assertNotEqual(first, research_scope(self.row,
            'What is tuition?', self.context))
        self.assertNotEqual(first, research_scope(self.row,
            'What are the TOEFL requirements?', self.context))
        self.assertNotEqual(first, research_scope(self.row,
            'What are the IELTS requirements for MS Mechanical Engineering?', self.context))
        self.assertEqual(research_scope(self.row,
            'IELTS requirements for MS Mechanical Engineering?', self.context),
            research_scope(self.row,
                'IELTS requirements for MS Mechanical Engineering?', self.context))

    def test_completion_is_shared_without_second_collection(self):
        scope = research_scope(self.row, 'IELTS requirements', self.context)
        called = []
        self.assertEqual(collect_once(self.row, scope, lambda owned: called.append(owned()) or True), 'completed')
        self.assertEqual(collect_once(self.row, scope, lambda owned: called.append('duplicate'), wait_seconds=0), 'recent')
        self.assertEqual(called, [True])
        self.assertEqual(PublicCollectionJob.objects.count(), 1)

    def test_shared_research_query_contains_public_scope_only(self):
        scope = research_scope(self.row, 'What are the IELTS requirements?', self.context)
        query = public_collection_query(scope, 'I am Ammar, my GPA is 9.1 and my email is a@example.com. What are the IELTS requirements?')
        self.assertIn('computer science', query)
        self.assertIn('ielts', query)
        self.assertNotIn('Ammar', query)
        self.assertNotIn('9.1', query)
        self.assertNotIn('@', query)

    def test_live_owner_is_joined_and_expired_owner_can_be_recovered(self):
        scope = research_scope(self.row, 'IELTS requirements', self.context)
        from university_research.collection_jobs import _claim
        job, token, status = _claim(self.row, scope)
        self.assertEqual(status, 'owned')
        self.assertEqual(collect_once(self.row, scope, lambda owned: self.fail('duplicate collection'),
            wait_seconds=0), 'busy')
        PublicCollectionJob.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(collect_once(self.row, scope, lambda owned: owned(), wait_seconds=0), 'completed')
        job.refresh_from_db()
        self.assertNotEqual(job.lease_token, token)
        self.assertEqual(job.status, 'completed')

    def test_failed_collection_retries_after_cooldown(self):
        scope = research_scope(self.row, 'IELTS requirements', self.context)
        with self.assertRaisesRegex(ValueError, 'fetch failed'):
            collect_once(self.row, scope, lambda owned: (_ for _ in ()).throw(ValueError('fetch failed')))
        self.assertEqual(collect_once(self.row, scope, lambda owned: self.fail('early retry'), wait_seconds=0), 'failed')
        PublicCollectionJob.objects.update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(collect_once(self.row, scope, lambda owned: True, wait_seconds=0), 'completed')

    def test_replaced_owner_cannot_report_shared_completion(self):
        scope = research_scope(self.row, 'IELTS requirements', self.context)

        def lose_lease(owned):
            self.assertTrue(owned())
            PublicCollectionJob.objects.update(lease_token=None)
            return True

        self.assertEqual(collect_once(self.row, scope, lose_lease, wait_seconds=0), 'busy')
        self.assertEqual(PublicCollectionJob.objects.get().status, 'running')

    def test_maintenance_removes_only_old_coordination_rows(self):
        from university_research.tasks import cleanup_collection_jobs
        scope = research_scope(self.row, 'IELTS requirements', self.context)
        self.assertEqual(collect_once(self.row, scope, lambda owned: True), 'completed')
        self.assertEqual(cleanup_collection_jobs(), 0)
        PublicCollectionJob.objects.update(updated_at=timezone.now()-timedelta(days=8))
        self.assertEqual(cleanup_collection_jobs(), 1)
