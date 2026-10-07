from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from institutes.services import register_institute
from .models import InstituteStudentList, ListedStudent


class RosterPaginationTests(TestCase):
    def setUp(self):
        self.institute = register_institute("Paging Institute", country="IN")
        self.other = register_institute("Other Paging Institute", country="IN")
        self.client = self._client("paging@example.edu", self.institute)
        self.other_client = self._client("other@example.edu", self.other)
        self.lists = []
        for index in range(30):
            lst = InstituteStudentList.objects.create(
                institute=self.institute, contact_name=f"Contact {index}",
                contact_email=f"contact{index}@example.edu", row_count=2,
                source_file_name=f"roster-{index}.csv",
            )
            self.lists.append(lst)
            ListedStudent.objects.create(
                source_list=lst, institute_id=str(self.institute.uuid),
                full_name=f"Student {index}", email=f"student{index}@example.edu",
                status=ListedStudent.Status.CLAIMED,
            )
            ListedStudent.objects.create(
                source_list=lst, institute_id=str(self.institute.uuid),
                full_name=f"Pending {index}", email=f"pending{index}@example.edu",
            )
        self.selected = self.lists[-1]
        self.foreign_list = InstituteStudentList.objects.create(
            institute=self.other, contact_name="Other", contact_email="other@example.edu", row_count=1
        )
        ListedStudent.objects.create(
            source_list=self.foreign_list, institute_id=str(self.other.uuid),
            full_name="Foreign student", email="foreign@example.edu",
        )

    def _client(self, email, institute):
        user = User.objects.create_user(username=email, email=email, password="test-password")
        Account.objects.create(user=user, role=Account.Role.INSTITUTE, institute=institute)
        TOTPDevice.objects.create(user=user, secret_encrypted="unused", confirmed_at=timezone.now())
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_summary_is_scoped_and_recent_items_are_bounded(self):
        response = self.client.get("/api/institute-lists/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["list_count"], 30)
        self.assertEqual(response.data["total_rows"], 60)
        self.assertEqual(response.data["claimed_count"], 30)
        self.assertEqual(response.data["unclaimed_count"], 30)
        self.assertEqual(len(response.data["recent_lists"]), 5)
        self.assertEqual(response.data["recent_lists"][0]["list_id"], self.selected.id)

    def test_list_index_paginates_and_searches_with_page_counts(self):
        first = self.client.get("/api/institute-lists/lists/")
        self.assertEqual(len(first.data["lists"]), 25)
        self.assertEqual(first.data["pagination"], {
            "page": 1, "page_size": 25, "total": 30, "has_next": True,
        })
        second = self.client.get("/api/institute-lists/lists/?page=2")
        self.assertEqual(len(second.data["lists"]), 5)
        self.assertFalse(second.data["pagination"]["has_next"])
        matched = self.client.get("/api/institute-lists/lists/?search=roster-29.csv")
        self.assertEqual(matched.data["pagination"]["total"], 1)
        self.assertEqual(matched.data["lists"][0]["list_id"], self.selected.id)
        self.assertEqual(matched.data["lists"][0]["claimed_count"], 1)
        self.assertEqual(self.client.get("/api/institute-lists/lists/?page=0").status_code, 400)

    def test_detail_counts_and_roster_filter_are_whole_list_scoped(self):
        more = [ListedStudent(
            source_list=self.selected, institute_id=str(self.institute.uuid),
            full_name=f"Extra {index}", email=f"extra{index}@example.edu",
        ) for index in range(30)]
        ListedStudent.objects.bulk_create(more)
        self.selected.row_count = 32
        self.selected.save(update_fields=["row_count"])
        detail_url = f"/api/institute-lists/lists/{self.selected.id}/"
        detail = self.client.get(detail_url)
        self.assertEqual(detail.data["list"]["unclaimed_count"], 31)
        self.assertEqual(detail.data["invite_counts"]["send_eligible"], 31)
        self.assertEqual(detail.data["invite_counts"]["resend_eligible"], 31)
        ListedStudent.objects.filter(source_list=self.selected, full_name="Pending 29").update(
            invited_at=timezone.now(), invite_delivery_status="sent"
        )
        ListedStudent.objects.filter(source_list=self.selected, full_name="Extra 0").update(
            invited_at=timezone.now(), invite_delivery_status="queued"
        )
        invite_counts = self.client.get(detail_url).data["invite_counts"]
        self.assertEqual(invite_counts, {"send_eligible": 29, "resend_eligible": 30, "queued": 1})

        roster_url = f"{detail_url}students/"
        first = self.client.get(roster_url)
        self.assertEqual(len(first.data["students"]), 25)
        self.assertEqual(first.data["pagination"]["total"], 32)
        self.assertTrue(first.data["pagination"]["has_next"])
        second = self.client.get(roster_url + "?page=2")
        self.assertEqual(len(second.data["students"]), 7)
        matched = self.client.get(roster_url + "?search=student+29&status=claimed")
        self.assertEqual(matched.data["pagination"]["total"], 1)
        self.assertEqual(matched.data["students"][0]["full_name"], "Student 29")
        self.assertEqual(self.client.get(roster_url + "?status=bad").status_code, 400)

    def test_other_institute_cannot_read_summary_detail_or_roster(self):
        self.assertEqual(self.other_client.get("/api/institute-lists/summary/").data["list_count"], 1)
        self.assertEqual(self.other_client.get("/api/institute-lists/lists/").data["pagination"]["total"], 1)
        detail_url = f"/api/institute-lists/lists/{self.selected.id}/"
        self.assertEqual(self.other_client.get(detail_url).status_code, 403)
        self.assertEqual(self.other_client.get(detail_url + "students/").status_code, 403)

    def test_superuser_can_scope_index_and_summary_by_institute(self):
        user = User.objects.create_user(username="root@example.edu", email="root@example.edu")
        Account.objects.create(user=user, role=Account.Role.SUPERUSER)
        TOTPDevice.objects.create(user=user, secret_encrypted="unused", confirmed_at=timezone.now())
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(f"/api/institute-lists/lists/?institute_id={self.institute.uuid}")
        self.assertEqual(response.data["pagination"]["total"], 30)
        summary = client.get(f"/api/institute-lists/summary/?institute_id={self.other.uuid}")
        self.assertEqual(summary.data["list_count"], 1)
        self.assertEqual(summary.data["total_rows"], 1)
        self.assertEqual(client.get(f"/api/institute-lists/lists/{self.foreign_list.id}/").status_code, 200)
