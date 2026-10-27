from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from accounts.models import Account
from agent_queries.models import AgentConversation, AgentQuery
from agent_queries.services import raise_query, answer_query
from django_api.models import StudentProfile
from universities.models import University, KnowledgeGroup
from notifications.models import NotificationLog, EmailDelivery, PushDelivery
from notifications.email_delivery import work_once


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class LifecycleNotificationTests(TestCase):
    def account(self, name, role, **kwargs):
        return Account.objects.create(user=User.objects.create_user(name, email=name + '@example.test'), role=role, **kwargs)

    def setUp(self):
        self.admin = self.account('admin', 'superuser')
        self.profile = StudentProfile.objects.create(name='Demo')
        self.student = self.account('student', 'student', student_profile=self.profile)
        self.school = University.objects.create(name='Franklin University')
        self.officer = self.account('officer', 'university', university=self.school)
        self.group = KnowledgeGroup.objects.create(university=self.school, slug='campus_life')
        self.staff = self.account('department', 'department', university=self.school)
        self.staff.departments.add(self.group)
        self.other_school = University.objects.create(name='Other')
        self.other = self.account('other', 'university', university=self.other_school)
        self.conv = AgentConversation.objects.create(student=self.profile, university=self.school)

    def test_registration_notifies_admin_once_and_email_is_asynchronous(self):
        self.student.save()
        log = NotificationLog.objects.get(account=self.admin)
        self.assertEqual(log.data['type'], 'student_joined')
        self.assertEqual(EmailDelivery.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(work_once())
        self.assertFalse(work_once())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/superuser/#/admin/students/', mail.outbox[0].body)

    def test_both_query_directions_and_answers_reach_the_correct_recipients(self):
        NotificationLog.objects.all().delete()
        for direction, recipient, responder in [
            (AgentQuery.Direction.STUDENT, self.officer, self.officer),
            (AgentQuery.Direction.UNIVERSITY, self.student, self.student),
        ]:
            with self.subTest(direction=direction):
                question = f'Housing assistance question {direction}'
                result = raise_query(self.conv, direction, question)
                count = NotificationLog.objects.count()
                raise_query(self.conv, direction, question)
                self.assertEqual(NotificationLog.objects.count(), count)
                self.assertTrue(NotificationLog.objects.filter(account=recipient, data__query_id=result['query_id'], data__status='unanswered').exists())
                answer_query(result['query_id'], responder, 'A verified response')
                target = self.student if direction == AgentQuery.Direction.STUDENT else self.officer
                log = NotificationLog.objects.get(account=target, data__query_id=result['query_id'], data__status='answered')
                self.assertTrue(EmailDelivery.objects.filter(notification=log).exists())
        self.assertFalse(NotificationLog.objects.filter(account=self.admin).exists())
        self.assertFalse(EmailDelivery.objects.filter(notification__account=self.admin).exists())
        self.assertEqual(EmailDelivery.objects.filter(notification__account=self.student).count(), 2)
        self.assertEqual(EmailDelivery.objects.filter(notification__account=self.officer).count(), 2)
        self.assertEqual(EmailDelivery.objects.filter(notification__account=self.staff).count(), 2)
        self.assertEqual(NotificationLog.objects.filter(account=self.staff).count(), 2)
        self.assertFalse(NotificationLog.objects.filter(account=self.other).exists())
        self.assertEqual(PushDelivery.objects.count(), 2)

    def test_failed_email_retries_without_duplicating_inbox_event(self):
        with patch('notifications.email_delivery.EmailMessage.send', side_effect=OSError('offline')):
            self.assertTrue(work_once())
        row = EmailDelivery.objects.get()
        self.assertIsNone(row.sent_at)
        self.assertEqual(row.attempts, 1)
        EmailDelivery.objects.update(available_at=timezone.now())
        self.assertTrue(work_once())
        self.assertFalse(work_once())
        self.assertEqual(NotificationLog.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_university_and_specific_department_receive_email_and_inbox(self):
        NotificationLog.objects.all().delete()
        unrelated_group = KnowledgeGroup.objects.create(university=self.school, slug='money')
        unrelated_staff = self.account('finance', 'department', university=self.school)
        unrelated_staff.departments.add(unrelated_group)
        raise_query(self.conv, AgentQuery.Direction.STUDENT, 'Who helps with housing?')
        self.assertFalse(NotificationLog.objects.filter(account=unrelated_staff).exists())
        self.assertTrue(NotificationLog.objects.filter(account=self.officer).exists())
        self.assertTrue(NotificationLog.objects.filter(account=self.staff).exists())
        self.assertEqual(EmailDelivery.objects.count(), 2)
        while work_once():
            pass
        self.assertEqual({recipient for message in mail.outbox for recipient in message.to},
                         {self.officer.user.email, self.staff.user.email})
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn(f'/university/#/university/{self.school.uuid}/agent-queries?query=', mail.outbox[0].body)

    def test_rollback_does_not_send_or_leave_an_event(self):
        NotificationLog.objects.all().delete()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.account('rollback', 'student')
            raise RuntimeError('rollback')
        self.assertFalse(NotificationLog.objects.exists())
        self.assertFalse(EmailDelivery.objects.exists())

    def test_disabled_recipient_is_not_emailed(self):
        self.admin.user.is_active = False
        self.admin.user.save()
        self.assertTrue(work_once())
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(work_once())
