from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from django_api.models import PendingQuery
from universities.models import KnowledgeGroup, University


class LegacyUniversityQueryPaginationTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Query University")
        self.other_university = University.objects.create(name="Other University")
        self.university_id = str(self.university.uuid)
        self.assigned = KnowledgeGroup.objects.create(
            university=self.university, slug=KnowledgeGroup.Slug.ADMISSIONS
        )
        self.unassigned = KnowledgeGroup.objects.create(
            university=self.university, slug=KnowledgeGroup.Slug.MONEY
        )
        self.staff = User.objects.create_user(
            username="query-staff", email="query-staff@example.edu", password="test-password"
        )
        staff_account = Account.objects.create(
            user=self.staff, role=Account.Role.DEPARTMENT, university=self.university
        )
        staff_account.departments.add(self.assigned)
        TOTPDevice.objects.create(
            user=self.staff, secret_encrypted="unused", confirmed_at=timezone.now()
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        for index in range(25):
            PendingQuery.objects.create(
                university_id=self.university_id, group=self.assigned,
                question=f"Question {index}", escalation_chain=[{"step": "large trace"}],
            )
        for index in range(4):
            PendingQuery.objects.create(
                university_id=self.university_id, group=self.assigned,
                question=f"Resolved {index}", status=PendingQuery.Status.RESOLVED,
                answer="Verified answer", answered_by="Officer",
            )
        PendingQuery.objects.create(
            university_id=self.university_id, group=self.unassigned, question="Hidden department"
        )
        PendingQuery.objects.create(university_id=self.university_id, question="No department")
        PendingQuery.objects.create(
            university_id=str(self.other_university.uuid), group=self.assigned,
            question="Other university",
        )

    def url(self, suffix=""):
        return f"/api/university/{self.university_id}/queries/{suffix}"

    def test_all_active_and_archive_are_bounded_and_department_scoped(self):
        first = self.client.get(self.url(), {"page_size": 10})
        second = self.client.get(self.url(), {"page": 2, "page_size": 10})
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["pagination"], {
            "page": 1, "page_size": 10, "total": 29, "has_next": True,
        })
        self.assertEqual(len(first.data["queries"]), 10)
        self.assertEqual(len(second.data["queries"]), 10)
        self.assertFalse({row["query_id"] for row in first.data["queries"]} &
                         {row["query_id"] for row in second.data["queries"]})
        self.assertEqual(first.data["queries"][0]["group"], self.assigned.slug)
        self.assertNotIn("escalation_chain", first.data["queries"][0])
        self.assertNotIn("confidence", first.data["queries"][0])

        active = self.client.get(self.url("active/"), {"page_size": 100}).data
        self.assertEqual(active["pagination"]["total"], 25)
        self.assertEqual(len(active["queries"]), 25)
        archive = self.client.get(self.url("archive/")).data
        self.assertEqual(archive["pagination"]["total"], 4)
        self.assertEqual(archive["queries"][0]["answer"], "Verified answer")
        self.assertEqual(archive["queries"][0]["display_status"], "answered")

    def test_university_admin_can_see_all_own_departments(self):
        admin = User.objects.create_user(username="query-admin", email="admin@example.edu")
        Account.objects.create(user=admin, role=Account.Role.UNIVERSITY, university=self.university)
        TOTPDevice.objects.create(
            user=admin, secret_encrypted="unused", confirmed_at=timezone.now()
        )
        self.client.force_authenticate(user=admin)
        response = self.client.get(self.url())
        self.assertEqual(response.data["pagination"]["total"], 31)
        self.assertEqual(len(response.data["queries"]), 20)

    def test_invalid_pagination_and_other_university_are_rejected(self):
        for params in ({"page": 0}, {"page": "abc"}, {"page_size": 0},
                       {"page_size": 101}, {"page_size": "abc"}):
            self.assertEqual(self.client.get(self.url(), params).status_code, 400)
        self.assertEqual(
            self.client.get(f"/api/university/{self.other_university.uuid}/queries/").status_code,
            403,
        )

    def test_group_serialization_does_not_query_once_per_row(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url("active/"), {"page_size": 20})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["queries"]), 20)
        self.assertLessEqual(len(queries), 8)
