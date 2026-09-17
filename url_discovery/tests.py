# url_discovery/tests.py
# B2: the human approval step between "crawler found these" and "these
# facts are in the knowledge base under a department".
from unittest import mock

from django.test import TestCase
from rest_framework import status

from django_api.tests import make_university_client
from universities.models import KnowledgeGroup, University
from url_discovery.models import DiscoveredUrl, DiscoveryClusterApproval, DiscoveryJob


class ClusterApprovalTests(TestCase):
    def setUp(self):
        self.client, self.university_id = make_university_client(
            email="officer_cluster@wsu.edu", university_id="cluster_state"
        )
        self.university = University.objects.get(name="cluster_state")
        self.job = DiscoveryJob.objects.create(
            university=self.university,
            base_url="https://cluster-state.edu/",
            root_domain="cluster-state.edu",
        )
        DiscoveredUrl.objects.create(
            job=self.job,
            original_url="https://cluster-state.edu/admissions/deadlines",
            normalized_url="https://cluster-state.edu/admissions/deadlines",
            primary_category="deadlines",
            relevance_score=0.9,
            decision_status="relevant",
        )
        DiscoveredUrl.objects.create(
            job=self.job,
            original_url="https://cluster-state.edu/tuition/fees",
            normalized_url="https://cluster-state.edu/tuition/fees",
            primary_category="fees",
            relevance_score=0.8,
            decision_status="relevant",
        )

    def test_clusters_endpoint_shows_proposed_department_map(self):
        resp = self.client.get(f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        categories = {c["category"] for c in resp.data["clusters"]}
        self.assertEqual(categories, {"deadlines", "fees"})

        fees_cluster = next(c for c in resp.data["clusters"] if c["category"] == "fees")
        self.assertEqual(fees_cluster["knowledge_group_slug"], "money")
        self.assertEqual(fees_cluster["url_count"], 1)
        self.assertIsNone(fees_cluster["approved"])

    @mock.patch("knowledge.scraper.scrape_university")
    def test_approving_a_cluster_records_provenance_and_applies_urls(self, mock_scrape):
        mock_scrape.return_value = 1

        resp = self.client.post(
            f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        approval = DiscoveryClusterApproval.objects.get(job=self.job, category="fees")
        self.assertEqual(approval.approved_by, "officer_cluster@wsu.edu")
        self.assertIsNotNone(approval.approved_at)
        self.assertEqual(approval.url_count, 1)

        # The category->group mapping is still recorded on the approval as
        # review metadata for the department map...
        money_group = KnowledgeGroup.objects.get(university=self.university, slug="money")
        self.assertEqual(approval.knowledge_group_id, money_group.id)

        self.university.refresh_from_db()
        self.assertIn("https://cluster-state.edu/tuition/fees", self.university.scrape_urls)

        # ...but the scraped facts themselves are NOT tagged into that group:
        # knowledge groups collect escalations, not auto-scraped knowledge.
        mock_scrape.assert_called_once()
        _args, kwargs = mock_scrape.call_args
        self.assertIsNone(kwargs.get("group_id"))

    @mock.patch("knowledge.scraper.scrape_university")
    def test_reapproving_a_cluster_refreshes_provenance_instead_of_duplicating(self, mock_scrape):
        mock_scrape.return_value = 1
        url = f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/"

        self.client.post(url)
        first = DiscoveryClusterApproval.objects.get(job=self.job, category="fees")

        self.client.post(url)
        self.assertEqual(DiscoveryClusterApproval.objects.filter(job=self.job, category="fees").count(), 1)
        second = DiscoveryClusterApproval.objects.get(job=self.job, category="fees")
        self.assertGreaterEqual(second.approved_at, first.approved_at)

    def test_approving_unknown_category_404s(self):
        resp = self.client.post(
            f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/not-a-category/approve/"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
