from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from cryptography.fernet import Fernet
from rest_framework.test import APIClient
from accounts.models import Account, TOTPDevice
from universities.models import University, KnowledgeGroup, StaffAuditEvent
from django_api.models import UniversityKnowledgeEntry, PendingQuery

@override_settings(TOTP_SECRET_KEYS=(Fernet.generate_key().decode(),), PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UniversityStaffTests(TestCase):
    def setUp(self):
        self.uni = University.objects.create(name="One")
        self.other = University.objects.create(name="Two")
        self.groups = {slug: KnowledgeGroup.objects.create(university=self.uni, slug=slug) for slug in KnowledgeGroup.Slug.values}
        self.owner = self.make_user("owner", self.uni)
        self.client = APIClient(); self.client.force_authenticate(self.owner)

    def make_user(self, role, university):
        user = User.objects.create_user(f"{role}-{university.pk}-{User.objects.count()}@example.com", password="StrongPassword!728")
        Account.objects.create(user=user, role="university", university=university, university_role=role)
        TOTPDevice.objects.create(user=user, secret="JBSWY3DPEHPK3PXP", confirmed_at=timezone.now())
        return user

    def test_owner_creates_scoped_staff_without_returning_password(self):
        response = self.client.post("/api/university-admin/staff/", {"email": "office@example.com", "name": "Office", "role": "financial_aid", "initial_password": "SaferInitial!728"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("password", response.content.decode())
        staff = User.objects.get(email="office@example.com")
        self.assertTrue(staff.check_password("SaferInitial!728"))
        self.assertEqual(staff.account.university_role, "financial_aid")
        self.assertFalse(response.data["totp_enrolled"])
        self.assertEqual(StaffAuditEvent.objects.count(), 1)
        self.client.force_authenticate(staff)
        self.assertEqual(self.client.get("/api/university-admin/knowledge-groups/").status_code, 403)

    def test_last_owner_and_cross_university_updates_are_blocked(self):
        response = self.client.patch(f"/api/university-admin/staff/{self.owner.pk}/", {"active": False}, format="json")
        self.assertEqual(response.status_code, 400)
        outsider = self.make_user("owner", self.other)
        self.assertEqual(self.client.patch(f"/api/university-admin/staff/{outsider.pk}/", {"role": "viewer"}, format="json").status_code, 404)

    def test_departments_are_isolated_and_cannot_manage_staff_or_university(self):
        for role, group in [("admissions", "admissions"), ("international", "international"), ("financial_aid", "money"), ("campus_life", "campus_life")]:
            with self.subTest(role=role):
                self.client.force_authenticate(self.make_user(role, self.uni))
                groups = self.client.get("/api/university-admin/knowledge-groups/")
                self.assertEqual([row["slug"] for row in groups.data["groups"]], [group])
                for slug in KnowledgeGroup.Slug.values:
                    response = self.client.get(f"/api/university-admin/knowledge-groups/{slug}/knowledge_list/")
                    self.assertEqual(response.status_code, 200 if slug == group else 403)
                self.assertEqual(self.client.get("/api/university-admin/staff/").status_code, 403)
                self.assertEqual(self.client.patch("/api/university-admin/profile/", {"name": "Changed"}, format="json").status_code, 403)
                self.assertEqual(self.client.post("/api/university-admin/knowledge/", {"topic": "X", "content": "X", "group": next(slug for slug in KnowledgeGroup.Slug.values if slug != group)}, format="json").status_code, 403)

    def test_read_only_viewer_and_scoped_knowledge(self):
        for slug in self.groups:
            UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid), topic=slug, content="Fact", group=self.groups[slug])
        self.client.force_authenticate(self.make_user("financial_aid", self.uni))
        data = self.client.get("/api/university-admin/knowledge/?group=admissions").data
        self.assertEqual(data["knowledge"], [])
        own = self.client.get("/api/university-admin/knowledge/").data["knowledge"]
        self.assertEqual([row["group"] for row in own], ["money"])
        self.assertEqual(self.client.patch(f'/api/university-admin/knowledge/{own[0]["id"]}/', {"content": "Updated"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f'/api/university-admin/knowledge/{own[0]["id"]}/', {"group": "admissions"}, format="json").status_code, 403)
        self.client.force_authenticate(self.make_user("viewer", self.uni))
        self.assertEqual(self.client.get("/api/university-admin/knowledge/").status_code, 200)
        self.assertEqual(self.client.patch("/api/university-admin/profile/", {"name": "No"}, format="json").status_code, 403)
        self.assertEqual(self.client.get(f"/api/university/{self.other.uuid}/queries/").status_code, 403)

    def test_department_can_only_resolve_its_own_escalations(self):
        own = PendingQuery.objects.create(university_id=str(self.uni.uuid), question="Aid?", group=self.groups["money"])
        other = PendingQuery.objects.create(university_id=str(self.uni.uuid), question="Entry?", group=self.groups["admissions"])
        user = self.make_user("financial_aid", self.uni); self.client.force_authenticate(user)
        self.assertEqual(self.client.post(f"/api/queries/{other.pk}/ignore/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/queries/{own.pk}/ignore/", {"ignored_by": "Spoofed"}, format="json").status_code, 200)
        own.refresh_from_db(); self.assertNotEqual(own.answered_by, "Spoofed")

    def test_role_changes_take_effect_for_existing_sessions(self):
        staff = self.make_user("owner", self.uni)
        response = self.client.patch(f"/api/university-admin/staff/{staff.pk}/", {"role": "viewer"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.client.force_authenticate(User.objects.get(pk=staff.pk))
        self.assertEqual(self.client.get("/api/university-admin/staff/").status_code, 403)
