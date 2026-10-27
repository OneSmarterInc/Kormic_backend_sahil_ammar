from django.test import TestCase
from accounts.models import Account
from notifications.models import NotificationLog
from notifications.tests import make_student_client


class SectionActivityTests(TestCase):
    def setUp(self):
        self.client, student_id = make_student_client(email='activity@example.com')
        self.account = Account.objects.get(student_profile__uuid=student_id)

    def log(self, query):
        return NotificationLog.objects.create(account=self.account, event_type='pending_query_resolved',
            title='Answer', body='Answer', data={'route': 'queries', 'query_id': query})

    def test_counts_include_unread_events_beyond_current_page(self):
        for query in range(12):
            self.log(query + 1)
        response = self.client.get('/api/notifications/?page_size=1')
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['unread_sections']['queries']['count'], 12)

    def test_viewing_one_answer_preserves_other_queries_and_newer_notifications(self):
        first = self.log(1)
        other = self.log(2)
        newer = self.log(1)
        response = self.client.post('/api/notifications/read-section/',
            {'section': 'queries', 'query_id': 1, 'before_id': first.pk}, format='json')
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db(); other.refresh_from_db(); newer.refresh_from_db()
        self.assertIsNotNone(first.read_at)
        self.assertIsNone(other.read_at)
        self.assertIsNone(newer.read_at)
        self.assertEqual(response.data['unread_sections']['queries']['count'], 2)

    def test_section_read_cannot_mark_another_students_notification(self):
        other_client, _ = make_student_client(email='other-activity@example.com')
        row = self.log(4)
        response = other_client.post('/api/notifications/read-section/',
            {'section': 'queries', 'query_id': 4, 'before_id': row.pk}, format='json')
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertIsNone(row.read_at)

    def test_chatbot_messages_do_not_light_up_a_tab(self):
        NotificationLog.objects.create(account=self.account, event_type='agent_reply', title='Old reply', body='Hi')
        response = self.client.get('/api/notifications/')
        self.assertFalse(any(item['count'] for item in response.data['unread_sections'].values()))
