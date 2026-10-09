from django.test import TestCase, override_settings
from django.core.cache import cache
from django_api.tests import make_university_client
from django_api.models import UniversityKnowledgeEntry, KnowledgeIndexWork
from knowledge.university_kb import UniversityKnowledgeBase


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class UniversityInformationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client, self.uid = make_university_client()
        self.fact = UniversityKnowledgeEntry.objects.create(
            university_id=self.uid, topic="Merit scholarship criteria",
            content="Scholarships require GPA 3.0.", source_type="scraped",
            source_url="https://example.edu/aid", details={"minimum_gpa": 3.0},
        )
        self.url = f"/api/university-admin/knowledge/{self.fact.pk}/"

    def test_structured_record_creation_updates_student_knowledge(self):
        response = self.client.post('/api/university-admin/information/', {
            'topic': 'Merit award', 'content': 'Minimum GPA: 3.8\nDuration: 4 years',
            'category': 'scholarships', 'details': {'minimum_gpa': '3.8', 'duration': '4 years', 'information_type': 'scholarships'},
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['source_type'], 'human_verified')
        entry = UniversityKnowledgeEntry.objects.get(pk=response.data['id'])
        self.assertEqual(entry.university_id, self.uid)
        self.assertEqual(entry.details['duration'], '4 years')
        self.assertTrue(any('3.8' in item.content for item in UniversityKnowledgeBase(self.uid, lazy=True).search('Merit award')))

    def test_invalid_structured_record_does_not_leave_partial_fact(self):
        before = UniversityKnowledgeEntry.objects.count()
        response = self.client.post('/api/university-admin/information/', {
            'topic': 'Invalid fee', 'content': 'Some fee', 'category': 'not-a-category', 'details': {},
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(UniversityKnowledgeEntry.objects.count(), before)

    def test_overview_form_updates_profile_and_knowledge_and_rejects_stale_save(self):
        from universities.models import University
        url = '/api/university-admin/information/overview/'
        current = self.client.get(url).data
        values = {**current['values'], 'name': 'New university name', 'admissions_office_address': '12 Campus Road'}
        response = self.client.patch(url, {'values': values, 'expected_revision': current['revision']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(University.objects.get(uuid=self.uid).name, 'New university name')
        row = UniversityKnowledgeEntry.objects.get(university_id=self.uid, details___information_overview=True)
        self.assertIn('12 Campus Road', row.content)
        self.assertEqual(row.source_type, 'human_verified')
        self.assertEqual(self.client.patch(url, {'values': values, 'expected_revision': current['revision']}, format='json').status_code, 409)
        changed = self.client.patch('/api/university-admin/profile/', {'name': 'Updated from profile'}, format='json')
        self.assertEqual(changed.status_code, 200)
        row.refresh_from_db()
        self.assertIn('Updated from profile', row.content)

    def test_overview_rejects_invalid_email_without_changing_profile(self):
        url = '/api/university-admin/information/overview/'
        current = self.client.get(url).data
        response = self.client.patch(url, {'values': {'contact_email': 'not-an-email'}, 'expected_revision': current['revision']}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(url).data, current)

    def test_edit_is_immediately_visible_to_cached_and_lazy_student_retrieval(self):
        cached = UniversityKnowledgeBase(self.uid)
        lazy = UniversityKnowledgeBase(self.uid, lazy=True)
        before = KnowledgeIndexWork.objects.get(university_id=self.uid).revision
        listed = self.client.get("/api/university-admin/knowledge/").data["knowledge"][0]
        self.assertEqual(listed["category"], "scholarships")
        response = self.client.patch(self.url, {
            "content": "Scholarships require GPA 3.8.", "details": {"minimum_gpa": 3.8},
            "category": "scholarships", "expected_revision": listed["revision"],
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source_type"], "human_verified")
        self.assertEqual(response.data["details"], {"minimum_gpa": 3.8})
        self.assertGreater(KnowledgeIndexWork.objects.get(university_id=self.uid).revision, before)
        for kb in (cached, lazy):
            found = kb.search("scholarship")[0]
            self.assertIn("3.8", found.content)
            self.assertNotIn("3.0", found.content)
            self.assertEqual(found.details["minimum_gpa"], 3.8)
        stale = self.client.patch(self.url, {"content": "Old edit", "expected_revision": listed["revision"]}, format="json")
        self.assertEqual(stale.status_code, 409)

    def test_rescrape_cannot_restore_corrected_topic_even_after_rename(self):
        self.client.patch(self.url, {"topic": "Updated merit criteria", "content": "GPA 3.8 required"}, format="json")
        kb = UniversityKnowledgeBase(self.uid)
        restored = kb.store("Merit scholarship criteria", "Scholarships require GPA 3.0.", "scraped", source_url=self.fact.source_url)
        self.assertEqual(restored.db_id, self.fact.pk)
        self.assertEqual(restored.content, "GPA 3.8 required")
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(university_id=self.uid).count(), 1)

    def test_other_category_and_scope_and_validation(self):
        unknown = UniversityKnowledgeEntry.objects.create(university_id=self.uid, topic="Miscellaneous", content="Something unique")
        other = UniversityKnowledgeEntry.objects.create(university_id="another-university", topic="Secret", content="Private")
        rows = self.client.get("/api/university-admin/knowledge/").data["knowledge"]
        self.assertEqual(next(row for row in rows if row["id"] == unknown.pk)["category"], "other")
        self.assertNotIn(other.pk, [row["id"] for row in rows])
        self.assertEqual(self.client.patch(f"/api/university-admin/knowledge/{other.pk}/", {"content": "Overwrite"}, format="json").status_code, 404)
        for payload in ({"category": "bogus"}, {"content": None}, {"topic": "x" * 501}, {"details": []}):
            self.assertEqual(self.client.patch(self.url, payload, format="json").status_code, 400)
        response = self.client.patch(self.url, {"category": "other"}, format="json")
        self.assertEqual(response.data["category"], "other")

    def test_researched_courses_are_editable_and_survive_recollection(self):
        from django.utils import timezone
        from universities.models import University
        from university_research.models import PublicUniversity, UniversityPage, UniversityCourse
        from university_research.services import retrieve
        university = University.objects.get(uuid=self.uid)
        public = PublicUniversity.objects.create(registered_university=university, identity_key="test-info",
            name=university.name, website="https://example.edu", fetched_at=timezone.now())
        page = UniversityPage.objects.create(university=public, url="https://example.edu/courses",
            content="MSc Computing tuition 10000", content_hash="test", fetched_at=timezone.now())
        course = UniversityCourse.objects.create(university=public, page=page, name="MSc Computing",
            tuition="10000", requirements="GPA 3.0", source_quote=page.content, fetched_at=timezone.now())
        rows = self.client.get("/api/university-admin/information/").data["knowledge"]
        item = next(row for row in rows if row["id"] == f"research:course:{course.pk}")
        self.assertTrue(any(row["id"] == f"research:page:{page.pk}" for row in rows))
        details = {**item["details"], "tuition": "12000", "requirements": "GPA 3.8"}
        response = self.client.patch(f"/api/university-admin/information/research/course/{course.pk}/", {
            "topic": item["topic"], "content": item["content"], "category": "academics",
            "details": details, "expected_revision": item["revision"],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("12000", response.data["content"])
        self.assertNotIn("10000", response.data["content"])
        course.delete()
        UniversityCourse.objects.create(university=public, page=page, name="MSc Computing",
            tuition="10000", requirements="GPA 3.0", source_quote=page.content, fetched_at=timezone.now())
        evidence = retrieve(public, "Computing tuition")
        self.assertEqual(evidence["courses"][0]["tuition"], "12000")
        self.assertEqual(evidence["courses"][0]["requirements"], "GPA 3.8")
        self.assertNotIn("10000", evidence["courses"][0]["source_quote"])
        rows = self.client.get("/api/university-admin/information/").data["knowledge"]
        self.assertFalse(any(str(row["id"]).startswith("research:course:") for row in rows))
        self.assertTrue(any(row["id"] == response.data["id"] for row in rows))

    def test_profile_sync_preserves_officer_correction(self):
        from universities.models import University
        from universities.services import sync_profile_facts_to_kb, PROGRAM_OVERVIEW_TOPIC
        university = University.objects.get(uuid=self.uid)
        university.description = "Old description"
        sync_profile_facts_to_kb(university)
        entry = UniversityKnowledgeEntry.objects.get(university_id=self.uid, topic=PROGRAM_OVERVIEW_TOPIC)
        self.client.patch(f"/api/university-admin/knowledge/{entry.pk}/", {"content": "Corrected description"}, format="json")
        sync_profile_facts_to_kb(university)
        entry.refresh_from_db()
        self.assertEqual(entry.content, "Corrected description")

    def test_page_correction_replaces_stale_extracted_evidence_and_rejects_stale_save(self):
        from django.utils import timezone
        from universities.models import University
        from university_research.models import PublicUniversity, UniversityPage, UniversityFact
        from university_research.services import retrieve
        university = University.objects.get(uuid=self.uid)
        public = PublicUniversity.objects.create(registered_university=university, identity_key="test-page",
            name=university.name, website="https://example.edu", fetched_at=timezone.now())
        page = UniversityPage.objects.create(university=public, url="https://example.edu/aid",
            title="Scholarships", content="GPA 3.0 required", content_hash="page", fetched_at=timezone.now())
        UniversityFact.objects.create(university=public, page=page, topic="Scholarships", content=page.content,
            source_quote=page.content, fetched_at=timezone.now())
        item = next(row for row in self.client.get("/api/university-admin/information/").data["knowledge"] if row["id"] == f"research:page:{page.pk}")
        url = f"/api/university-admin/information/research/page/{page.pk}/"
        payload = {"content": "Scholarships require GPA 3.9", "expected_revision": item["revision"]}
        self.assertEqual(self.client.patch(url, payload, format="json").status_code, 200)
        self.assertEqual(self.client.patch(url, payload, format="json").status_code, 409)
        evidence = retrieve(public, "scholarships")
        self.assertEqual(evidence["facts"], [])
        self.assertEqual(evidence["saved_knowledge"][0]["content"], "Scholarships require GPA 3.9")
        outsider, _ = make_university_client(email="other@example.edu", university_id="Other Institute")
        self.assertEqual(outsider.patch(url, payload, format="json").status_code, 404)
