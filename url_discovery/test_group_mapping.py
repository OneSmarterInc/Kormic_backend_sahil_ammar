from unittest.mock import patch

from django.test import TestCase

from universities.models import University
from url_discovery.models import DiscoveredUrl, DiscoveryJob
from url_discovery.services import approve_cluster


class ClassifierLabelApprovalTests(TestCase):
    @patch("celery.current_app.send_task")
    def test_display_labels_route_approvals_to_all_four_groups(self, send_task):
        cases = [
            ("Admissions & Application", "admissions"),
            ("International Students & Visa", "international"),
            ("Fees, Tuition & Financial Aid", "money"),
            ("Housing, Dining & Campus Life", "campus_life"),
        ]
        for index, (label, expected) in enumerate(cases):
            with self.subTest(label=label):
                university = University.objects.create(name=f"Demo {index}")
                job = DiscoveryJob.objects.create(
                    university=university,
                    base_url="https://example.edu/",
                    root_domain="example.edu",
                )
                url = f"https://example.edu/source-{index}"
                DiscoveredUrl.objects.create(
                    job=job, original_url=url, normalized_url=url,
                    primary_category=label, decision_status="relevant",
                )
                approve_cluster(job, label, approved_by="test-reviewer")
                approval = job.cluster_approvals.get(category=label)
                self.assertEqual(approval.knowledge_group.slug, expected)
                self.assertEqual(approval.urls, [url])
        self.assertEqual(send_task.call_count, 4)
