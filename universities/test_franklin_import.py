from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from django_api.models import UniversityKnowledgeEntry
from universities.management.commands.import_franklin_fall2026 import SCHOOL, URL
from universities.models import KnowledgeGroup, University
from url_discovery.models import DiscoveryJob


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class FranklinPolicyImportTests(TestCase):
    def setUp(self):
        self.school = University.objects.create(uuid=SCHOOL, name="Franklin University")
        KnowledgeGroup.objects.create(university=self.school, slug="money")

    def run_import(self, snapshot):
        with patch("universities.management.commands.import_franklin_fall2026.request_with_policy",
                   return_value=snapshot):
            call_command("import_franklin_fall2026", approved_by="Test operator", stdout=StringIO())

    def test_import_preserves_policy_and_real_provenance_without_duplicate_rows(self):
        html = (b"<html><main><h1>Tuition changes Fall 2026</h1>"
                b"New master students pay $690 per credit. Tuition Guarantee: three years (9 terms). "
                b"Current master students keep their rate for three additional years starting Fall 2026."
                b"</main></html>")
        snapshot = (200, {"content-type": "text/html"}, html, URL)
        self.run_import(snapshot)
        ids = list(UniversityKnowledgeEntry.objects.values_list("pk", flat=True))
        self.run_import(snapshot)
        self.assertEqual(list(UniversityKnowledgeEntry.objects.values_list("pk", flat=True)), ids)
        self.assertEqual(DiscoveryJob.objects.count(), 1)
        job = DiscoveryJob.objects.get()
        self.assertEqual(job.cluster_approvals.get().knowledge_group.slug, "money")
        self.assertEqual(job.cluster_approvals.get().approved_by, "Test operator")
        self.assertEqual(job.urls.get().html_snapshot, html.decode())
        self.assertEqual(job.urls.get().http_status, 200)
        self.assertIn("three additional years", job.settings["text_snapshot"])
        entry = UniversityKnowledgeEntry.objects.get()
        self.assertIn("three years (9 terms)", entry.content)
        self.assertEqual(entry.source_url, URL)

    def test_http_failure_writes_no_knowledge_or_approval(self):
        with self.assertRaises(CommandError):
            self.run_import((403, {"content-type": "text/html"}, b"Denied", URL))
        self.assertFalse(UniversityKnowledgeEntry.objects.exists())
        self.assertFalse(DiscoveryJob.objects.exists())

    def test_challenge_page_or_unexpected_redirect_is_not_policy_evidence(self):
        for body, url in ((b"<main>Please verify you are human</main>", URL),
                          (b"<main>Fall 2026 master Tuition Guarantee</main>", "https://www.franklin.edu/")):
            with self.assertRaises(CommandError):
                self.run_import((200, {"content-type": "text/html"}, body, url))
        self.assertFalse(DiscoveryJob.objects.exists())
        self.assertFalse(UniversityKnowledgeEntry.objects.exists())
