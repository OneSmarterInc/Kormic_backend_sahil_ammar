from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from agent_queries.models import AgentConversation, AgentQuery
from django_api.models import (
    PendingQuery,
    StudentProfile,
    UniversityInterestEvent,
    UniversityKnowledgeEntry,
)
from universities.models import KnowledgeGroup, University


class UniversityDashboardSummaryTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Dashboard University")
        self.other_university = University.objects.create(name="Other University")
        self.university_id = str(self.university.uuid)
        self.assigned = KnowledgeGroup.objects.create(
            university=self.university, slug=KnowledgeGroup.Slug.ADMISSIONS
        )
        self.other_group = KnowledgeGroup.objects.create(
            university=self.university, slug=KnowledgeGroup.Slug.MONEY
        )
        self.university_client = self._client("university", "admin@example.edu")
        self.department_client = self._client("department", "staff@example.edu")
        Account.objects.get(user__email="staff@example.edu").departments.add(self.assigned)

    def _client(self, role, email):
        user = User.objects.create_user(username=email, email=email, password="test-password")
        Account.objects.create(user=user, role=role, university=self.university)
        TOTPDevice.objects.create(user=user, secret_encrypted="unused", confirmed_at=timezone.now())
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _url(self, university=None):
        return f"/api/university/{university or self.university_id}/dashboard-summary/"

    def test_counts_all_interested_students_and_both_unanswered_query_streams(self):
        students = StudentProfile.objects.bulk_create(
            [StudentProfile(name=f"Student {index}") for index in range(61)]
        )
        UniversityInterestEvent.objects.bulk_create([
            UniversityInterestEvent(
                student=student, university_id=self.university_id,
                source=UniversityInterestEvent.Source.SEARCHED,
            ) for student in students
        ])
        UniversityInterestEvent.objects.create(
            student=students[0], university_id=self.university_id,
            source=UniversityInterestEvent.Source.FIT_CHECK,
        )
        UniversityInterestEvent.objects.create(
            student=StudentProfile.objects.create(name="Other interested student"),
            university_id=str(self.other_university.uuid),
            source=UniversityInterestEvent.Source.SEARCHED,
        )
        for index in range(2):
            UniversityKnowledgeEntry.objects.create(
                university_id=self.university_id, topic=f"Fact {index}", content="A fact"
            )
        UniversityKnowledgeEntry.objects.create(
            university_id=str(self.other_university.uuid), topic="Other fact", content="A fact"
        )
        for group, state in ((self.assigned, "pending"), (self.other_group, "pending"),
                             (self.assigned, "resolved")):
            PendingQuery.objects.create(
                university_id=self.university_id, group=group, question="A question", status=state
            )
        PendingQuery.objects.create(
            university_id=str(self.other_university.uuid), question="Other university question"
        )
        conversation = AgentConversation.objects.create(
            student=students[0], university=self.university
        )
        for index, (group, direction, state) in enumerate((
            (self.assigned, AgentQuery.Direction.STUDENT, "unanswered"),
            (self.other_group, AgentQuery.Direction.STUDENT, "unanswered"),
            (self.assigned, AgentQuery.Direction.STUDENT, "answered"),
            (self.assigned, AgentQuery.Direction.UNIVERSITY, "unanswered"),
        )):
            AgentQuery.objects.create(
                conversation=conversation, group=group, direction=direction,
                question=f"Question {index}", question_hash=f"hash-{index}",
                raised_by_agent="Student agent", recipient_agent="University agent", status=state,
            )

        response = self.university_client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "university_id": self.university_id,
            "pending_tasks": 4,
            "knowledge_facts": 2,
            "student_profiles": 61,
        })

        department_response = self.department_client.get(self._url())
        self.assertEqual(department_response.status_code, 200)
        self.assertEqual(department_response.data, {
            "university_id": self.university_id,
            "pending_tasks": 2,
        })

    def test_scope_blocks_other_university_and_students(self):
        self.assertEqual(
            self.university_client.get(self._url(str(self.other_university.uuid))).status_code,
            403,
        )
        self.assertEqual(
            self.department_client.get(self._url(str(self.other_university.uuid))).status_code,
            403,
        )
        student = User.objects.create_user(username="student@example.com", email="student@example.com")
        Account.objects.create(user=student, role=Account.Role.STUDENT)
        TOTPDevice.objects.create(user=student, secret_encrypted="unused", confirmed_at=timezone.now())
        student_client = APIClient()
        student_client.force_authenticate(user=student)
        self.assertEqual(student_client.get(self._url()).status_code, 403)
