# url_discovery/tests.py
# B2: the human approval step between "crawler found these" and "these
# facts are in the knowledge base under a department".
from unittest import mock

from django.test import TestCase
from rest_framework import status

from django_api.tests import make_university_client
from universities.models import KnowledgeGroup, ScrapeJob, University
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

    @mock.patch("celery.current_app.send_task")
    @mock.patch("universities.services.scrape_selected_urls")
    def test_approving_a_cluster_queues_scrape_and_records_provenance(self, mock_scrape, mock_send):

        resp = self.client.post(
            f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/"
        )

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)

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

        scrape_job = ScrapeJob.objects.get(id=resp.data["scrape_job"]["id"])
        self.assertEqual(scrape_job.cluster_approval_id, approval.id)
        self.assertEqual(scrape_job.scope, ScrapeJob.Scope.SELECTED)
        self.assertEqual(scrape_job.selected_urls, ["https://cluster-state.edu/tuition/fees"])
        self.assertEqual(resp.data["scrape_job"]["status"], "queued")
        self.assertEqual(resp.data["scrape_job"]["progress_total"], 1)
        self.assertEqual(
            self.client.get(f"/api/university-admin/scrape-urls/scrape-now/{scrape_job.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        mock_scrape.assert_not_called()
        mock_send.assert_called_once_with("universities.tasks.run_scrape_now_job", args=[scrape_job.id], retry=False)

    @mock.patch("celery.current_app.send_task")
    def test_duplicate_approval_while_queued_returns_same_job(self, mock_send):
        url = f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/"

        first_response = self.client.post(url)
        first = DiscoveryClusterApproval.objects.get(job=self.job, category="fees")

        second_response = self.client.post(url)
        self.assertEqual(second_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(DiscoveryClusterApproval.objects.filter(job=self.job, category="fees").count(), 1)
        self.assertEqual(ScrapeJob.objects.filter(cluster_approval=first).count(), 1)
        self.assertEqual(first_response.data["scrape_job"]["id"], second_response.data["scrape_job"]["id"])
        second = DiscoveryClusterApproval.objects.get(job=self.job, category="fees")
        self.assertEqual(second.approved_at, first.approved_at)
        mock_send.assert_called_once()

    @mock.patch("celery.current_app.send_task")
    def test_failed_cluster_scrape_can_retry_with_new_job(self, mock_send):
        url = f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/"
        first = self.client.post(url)
        ScrapeJob.objects.filter(pk=first.data["scrape_job"]["id"]).update(status="failed", error_message="Worker failed")

        cluster_map = self.client.get(f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/")
        fees = next(c for c in cluster_map.data["clusters"] if c["category"] == "fees")
        self.assertEqual(fees["approved"]["scrape_job"]["status"], "failed")
        self.assertEqual(fees["approved"]["scrape_job"]["error_message"], "Worker failed")

        retried = self.client.post(url)
        self.assertEqual(retried.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotEqual(first.data["scrape_job"]["id"], retried.data["scrape_job"]["id"])
        self.assertEqual(DiscoveryClusterApproval.objects.filter(job=self.job, category="fees").count(), 1)
        self.assertEqual(mock_send.call_count, 2)

    @mock.patch("celery.current_app.send_task")
    def test_other_active_scrape_blocks_cluster_approval(self, mock_send):
        ScrapeJob.objects.create(university=self.university)
        resp = self.client.post(f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(DiscoveryClusterApproval.objects.exists())
        mock_send.assert_not_called()

    @mock.patch("celery.current_app.send_task", side_effect=ConnectionError("broker unavailable"))
    def test_queue_failure_is_recorded_and_can_be_retried(self, _mock_send):
        resp = self.client.post(f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/")
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(ScrapeJob.objects.get(id=resp.data["scrape_job"]["id"]).status, "failed")
        self.assertTrue(DiscoveryClusterApproval.objects.filter(job=self.job, category="fees").exists())

    @mock.patch("notifications.services.notify_university")
    @mock.patch("knowledge.scraper.scrape_university")
    @mock.patch("celery.current_app.send_task")
    def test_worker_scrapes_only_snapshot_once_without_group_tag(self, mock_send, mock_scrape, _notify):
        from universities.tasks import run_scrape_now_job

        resp = self.client.post(f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/fees/approve/")
        scrape_job_id = resp.data["scrape_job"]["id"]
        mock_scrape.return_value = 2
        run_scrape_now_job(scrape_job_id)
        run_scrape_now_job(scrape_job_id)

        scrape_job = ScrapeJob.objects.get(id=scrape_job_id)
        self.assertEqual(scrape_job.status, "completed")
        self.assertEqual(scrape_job.result["total_facts_stored"], 2)
        self.assertEqual(scrape_job.progress_completed, 1)
        self.assertEqual(scrape_job.progress_total, 1)
        self.assertEqual(scrape_job.current_url, "")
        mock_scrape.assert_called_once()
        args, kwargs = mock_scrape.call_args
        self.assertEqual(args[1], ["https://cluster-state.edu/tuition/fees"])
        self.assertIsNone(kwargs.get("group_id"))

    def test_approving_unknown_category_404s(self):
        resp = self.client.post(
            f"/api/university-admin/scrape-urls/auto-discover/{self.job.id}/clusters/not-a-category/approve/"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
