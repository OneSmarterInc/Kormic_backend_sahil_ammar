from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework import status

from django_api.models import PendingQuery
from django_api.tests import make_university_client
from universities.knowledge_groups import (
    classify_group_slug,
    ensure_default_groups,
    escalation_counts_by_group,
    resolve_group_for_question,
)
from universities.models import KnowledgeGroup, ScrapeJob, University


class KnowledgeGroupClassificationTests(TestCase):
    def test_classifies_international_keywords(self):
        self.assertEqual(
            classify_group_slug("Do I need a new I-20 for CPT?"), KnowledgeGroup.Slug.INTERNATIONAL
        )

    def test_classifies_money_keywords(self):
        self.assertEqual(
            classify_group_slug("What is the assistantship stipend?"), KnowledgeGroup.Slug.MONEY
        )

    def test_classifies_campus_life_keywords(self):
        self.assertEqual(
            classify_group_slug("Is there a meal plan option in the dining hall?"), KnowledgeGroup.Slug.CAMPUS_LIFE
        )

    def test_defaults_to_admissions_when_nothing_matches(self):
        self.assertEqual(classify_group_slug("What is the application deadline?"), KnowledgeGroup.Slug.ADMISSIONS)
        self.assertEqual(classify_group_slug(""), KnowledgeGroup.Slug.ADMISSIONS)
        self.assertEqual(classify_group_slug("random unrelated text"), KnowledgeGroup.Slug.ADMISSIONS)


class KnowledgeGroupResolutionTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Write State")

    def test_no_groups_configured_returns_none(self):
        self.assertIsNone(resolve_group_for_question(str(self.university.uuid), "What is the tuition?"))

    def test_resolves_matching_group(self):
        ensure_default_groups(self.university)

        group = resolve_group_for_question(str(self.university.uuid), "What is the assistantship stipend?")

        self.assertEqual(group.slug, KnowledgeGroup.Slug.MONEY)

    def test_falls_back_to_first_configured_group_when_matching_slug_missing(self):
        KnowledgeGroup.objects.create(
            university=self.university,
            slug=KnowledgeGroup.Slug.CAMPUS_LIFE,
            escalation_contact_email="life@wsu.edu",
        )

        group = resolve_group_for_question(str(self.university.uuid), "What is the assistantship stipend?")

        self.assertEqual(group.slug, KnowledgeGroup.Slug.CAMPUS_LIFE)

    def test_ensure_default_groups_is_idempotent(self):
        created_first = ensure_default_groups(self.university)
        self.assertEqual(len(created_first), 4)

        created_second = ensure_default_groups(self.university)

        self.assertEqual(created_second, [])
        self.assertEqual(self.university.knowledge_groups.count(), 4)


class EscalationCountsTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Write State", contact_email="admissions@wsu.edu"
        )
        ensure_default_groups(self.university)

    def test_counts_by_group(self):
        money_group = self.university.knowledge_groups.get(slug=KnowledgeGroup.Slug.MONEY)
        PendingQuery.objects.create(university_id=str(self.university.uuid), question="stipend?", group=money_group)
        PendingQuery.objects.create(university_id=str(self.university.uuid), question="stipend again?", group=money_group)

        counts = escalation_counts_by_group(str(self.university.uuid))

        self.assertEqual(counts[KnowledgeGroup.Slug.MONEY], 2)
        self.assertEqual(counts[KnowledgeGroup.Slug.ADMISSIONS], 0)


class KnowledgeGroupAPITests(TestCase):
    def setUp(self):
        self.client, self.university_id = make_university_client(email="officer1@wsu.edu", university_id="write_state_api")

    def test_get_lazily_bootstraps_four_groups(self):
        resp = self.client.get("/api/university-admin/knowledge-groups/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {g["slug"] for g in resp.data["groups"]}
        self.assertEqual(slugs, {"admissions", "international", "money", "campus_life"})
        self.assertTrue(all(g["escalation_count"] == 0 for g in resp.data["groups"]))

    def test_patch_updates_contact(self):
        self.client.get("/api/university-admin/knowledge-groups/")

        resp = self.client.patch(
            "/api/university-admin/knowledge-groups/money/",
            {"escalation_contact_name": "Bursar Office", "escalation_contact_email": "bursar@wsu.edu"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["escalation_contact_email"], "bursar@wsu.edu")
        self.assertEqual(resp.data["escalation_contact_name"], "Bursar Office")

    def test_patch_unknown_group_404s(self):
        resp = self.client.patch(
            "/api/university-admin/knowledge-groups/not-a-real-group/",
            {"escalation_contact_name": "x"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def _seed_two_group_escalations(self):
        self.client.get("/api/university-admin/knowledge-groups/")
        university = University.objects.get(name="write_state_api")
        money = university.knowledge_groups.get(slug=KnowledgeGroup.Slug.MONEY)
        admissions = university.knowledge_groups.get(slug=KnowledgeGroup.Slug.ADMISSIONS)

        PendingQuery.objects.create(
            university_id=str(university.uuid),
            question="Is the stipend taxed?",
            group=money,
            student_name="Ada Lovelace",
            student_id="stu-1",
            agent_name="wsu-agent",
            escalation_chain=["wsu-agent", "money"],
        )
        PendingQuery.objects.create(
            university_id=str(university.uuid), question="deadline?", group=admissions
        )
        return university, money

    def test_escalations_list_endpoint_returns_only_this_groups_escalations(self):
        self._seed_two_group_escalations()

        resp = self.client.get("/api/university-admin/knowledge-groups/money/escalations_list/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data.keys()), {"group", "escalations"})
        self.assertEqual(resp.data["group"]["escalation_count"], 1)
        # Only the escalation routed to "money" comes back, not the admissions one.
        self.assertEqual(len(resp.data["escalations"]), 1)
        escalation = resp.data["escalations"][0]
        self.assertEqual(escalation["question"], "Is the stipend taxed?")
        self.assertEqual(escalation["student_name"], "Ada Lovelace")
        self.assertEqual(escalation["agent_name"], "wsu-agent")
        self.assertEqual(escalation["group"], "money")

    def test_knowledge_list_endpoint_has_no_escalations_key(self):
        university, _money = self._seed_two_group_escalations()

        resp = self.client.get("/api/university-admin/knowledge-groups/money/knowledge_list/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data.keys()), {"group", "knowledge"})
        self.assertNotIn("escalations", resp.data)


class ManualKnowledgeFactGroupTaggingTests(TestCase):
    def setUp(self):
        self.client, self.university_id = make_university_client(email="officer2@wsu.edu", university_id="write_state_facts")
        self.university = University.objects.get(name="write_state_facts")
        ensure_default_groups(self.university)

    def test_post_tags_fact_with_group(self):
        resp = self.client.post(
            "/api/university-admin/knowledge/",
            {"topic": "Assistantship stipend", "content": "$20,000/year", "group": "money"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["group"], "money")

    def test_post_rejects_unknown_group(self):
        resp = self.client.post(
            "/api/university-admin/knowledge/",
            {"topic": "x", "content": "y", "group": "not-a-real-group"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_without_group_leaves_fact_ungrouped(self):
        resp = self.client.post(
            "/api/university-admin/knowledge/",
            {"topic": "x", "content": "y"},
            format="json",
        )

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(resp.data["group"])


class ScrapeNowJobTests(TestCase):
    """
    ScrapeNowAPIView used to run services.scrape_now() synchronously in the
    request -- for a university with many scrape_urls this could block a web
    worker for minutes (each URL costs a be-polite time.sleep(1.5) plus an
    LLM extraction call). It now queues a Celery job and returns immediately;
    these tests check the queuing/polling contract, not the scrape itself
    (that's exercised wherever services.scrape_now already has coverage).
    """

    def setUp(self):
        self.client, self.university_id = make_university_client(email="officer3@wsu.edu", university_id="write_state_scrape")
        self.university = University.objects.get(name="write_state_scrape")
        self.university.scrape_urls = ["https://write-state.example/admissions"]
        self.university.save(update_fields=["scrape_urls"])

    @mock.patch("universities.tasks.run_scrape_now_job.delay")
    def test_post_queues_job_and_returns_202(self, mock_delay):
        resp = self.client.post("/api/university-admin/scrape-urls/scrape-now/")

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(resp.data["status"], ScrapeJob.Status.QUEUED)
        job = ScrapeJob.objects.get(id=resp.data["id"])
        self.assertEqual(job.university_id, self.university.id)
        mock_delay.assert_called_once_with(job.id)

    @mock.patch("universities.tasks.run_scrape_now_job.delay")
    def test_post_rejects_second_job_while_one_is_active(self, mock_delay):
        self.client.post("/api/university-admin/scrape-urls/scrape-now/")

        resp = self.client.post("/api/university-admin/scrape-urls/scrape-now/")

        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(mock_delay.call_count, 1)

    def test_post_without_scrape_urls_400s(self):
        self.university.scrape_urls = []
        self.university.save(update_fields=["scrape_urls"])

        resp = self.client.post("/api/university-admin/scrape-urls/scrape-now/")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("universities.tasks.run_scrape_now_job.delay")
    def test_job_detail_polling_reflects_status(self, mock_delay):
        resp = self.client.post("/api/university-admin/scrape-urls/scrape-now/")
        job_id = resp.data["id"]

        detail = self.client.get(f"/api/university-admin/scrape-urls/scrape-now/{job_id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data["status"], ScrapeJob.Status.QUEUED)

        job = ScrapeJob.objects.get(id=job_id)
        job.status = ScrapeJob.Status.COMPLETED
        job.result = {"total_facts_stored": 3, "results": []}
        job.save(update_fields=["status", "result"])

        detail = self.client.get(f"/api/university-admin/scrape-urls/scrape-now/{job_id}/")
        self.assertEqual(detail.data["status"], ScrapeJob.Status.COMPLETED)
        self.assertEqual(detail.data["result"]["total_facts_stored"], 3)
