from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.exceptions import PermissionDenied
from accounts.models import Account
from django_api.models import StudentProfile
from universities.models import University
from agent_queries.models import AgentQuery, AgentConversation
from agent_queries.services import answer_query


class AnswerOwnershipTests(TestCase):
    def setUp(self):
        student = StudentProfile.objects.create(name='Test Student')
        university = University.objects.create(name='Test University')
        self.student = Account.objects.create(user=User.objects.create_user(username='student'), role='student', student_profile=student)
        self.university = Account.objects.create(user=User.objects.create_user(username='university'), role='university', university=university)
        self.conversation = AgentConversation.objects.create(student=student, university=university)

    def query(self, direction):
        return AgentQuery.objects.create(conversation=self.conversation, direction=direction,
            question='What information is missing?', question_hash=direction, raised_by_agent='Test agent', recipient_agent='Other agent')

    def test_university_answers_student_question(self):
        row = self.query(AgentQuery.Direction.STUDENT)
        with self.assertRaises(PermissionDenied):
            answer_query(row.pk, self.student, 'Wrong party')
        answer_query(row.pk, self.university, 'University answer')
        row.refresh_from_db()
        self.assertEqual(row.status, 'answered')

    def test_student_answers_university_question(self):
        row = self.query(AgentQuery.Direction.UNIVERSITY)
        with self.assertRaises(PermissionDenied):
            answer_query(row.pk, self.university, 'Wrong party')
        answer_query(row.pk, self.student, 'Student answer')
        row.refresh_from_db()
        self.assertEqual(row.answer, 'Student answer')
