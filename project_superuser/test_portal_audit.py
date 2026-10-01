from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from project_superuser.tests import make_superuser_client
from project_superuser.models import ActivityLog


class PortalAuditRegressionTests(TestCase):
    def setUp(self):
        self.client = make_superuser_client()

    def test_invalid_filters_return_400(self):
        for params in ({'limit': -1}, {'limit': 0}, {'limit': 'bad'}, {'user_id': 'bad'}, {'before_id': -1}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get('/api/superuser/audit-log/', params).status_code, 400)

    def test_email_search_finds_older_records_and_cursor_does_not_repeat(self):
        old = ActivityLog.objects.create(action='login_failed', target_email='wanted@example.test')
        for i in range(5):
            ActivityLog.objects.create(action='login_failed', target_email=f'other{i}@example.test')
        result = self.client.get('/api/superuser/audit-log/', {'email': 'wanted', 'limit': 1}).data
        self.assertEqual([row['id'] for row in result['entries']], [old.id])
        first = self.client.get('/api/superuser/audit-log/', {'limit': 2}).data
        second = self.client.get('/api/superuser/audit-log/', {'limit': 2, 'before_id': first['next_cursor']}).data
        self.assertTrue(first['has_more'])
        self.assertFalse({r['id'] for r in first['entries']} & {r['id'] for r in second['entries']})

    def test_dashboard_summary_counts_and_query_budget(self):
        for i in range(4):
            self.client.post('/api/superuser/students/', {'email': f'student{i}@example.test', 'password': 'S3curePassw0rd!', 'name': f'Student {i}'}, format='json')
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/superuser/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['counts']['students'], 4)
        self.assertEqual(len(response.data['students']), 3)
        self.assertLessEqual(len(queries), 12)

    def test_lists_paginate_and_reject_invalid_pages(self):
        for endpoint, key in [('students', 'students'), ('users', 'users'), ('universities', 'universities')]:
            response = self.client.get(f'/api/superuser/{endpoint}/', {'page': 1, 'page_size': 1})
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(len(response.data[key]), 1)
            self.assertIn('pagination', response.data)
            self.assertEqual(self.client.get(f'/api/superuser/{endpoint}/', {'page': -1}).status_code, 400)

    def test_dashboard_requires_authentication(self):
        self.assertEqual(APIClient().get('/api/superuser/dashboard/').status_code, 401)
