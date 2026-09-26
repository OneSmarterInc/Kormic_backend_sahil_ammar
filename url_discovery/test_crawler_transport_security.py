from unittest import mock

import httpx
from django.test import TestCase

from universities.models import University
from url_discovery.crawler import DirectUniversityCrawler
from url_discovery.models import DiscoveredUrl, DiscoveryJob


class CrawlerTransportSecurityTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Security Test University", country="US")
        self.job = DiscoveryJob.objects.create(
            university=self.university,
            base_url="https://security.example/",
            root_domain="security.example",
            settings={"respect_robots_txt": False},
        )
        self.url = "https://security.example/"
        DiscoveredUrl.objects.create(
            job=self.job,
            original_url=self.url,
            normalized_url=self.url,
        )
        self.crawler = DirectUniversityCrawler(self.job.id)

    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    def test_html_page_is_stored_and_navigation_discovered_after_redirect(self, _mock_dns):
        final_url = self.url + "home/"

        def handler(request):
            if str(request.url) == self.url:
                return httpx.Response(302, headers={"Location": final_url}, request=request)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text='<html><head><title>University admissions</title></head>'
                     '<body><nav><a href="/admissions/">Admissions</a></nav>'
                     '<main><h1>Graduate admissions</h1><p>Applications are open.</p>'
                     '</main></body></html>',
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            self.crawler._fetch_and_parse(client, self.url, None, "", 0, None)

        row = DiscoveredUrl.objects.get(job=self.job, normalized_url=self.url)
        self.assertEqual(row.http_status, 200)
        self.assertEqual(row.final_url, final_url.rstrip("/"))
        self.assertEqual(row.page_title, "University admissions")
        self.assertEqual(row.h1, "Graduate admissions")
        self.assertIsNotNone(row.crawled_at)
        self.job.refresh_from_db()
        self.assertEqual(self.job.pages_crawled, 1)
        self.assertEqual(self.job.failed_count, 0)
        self.assertTrue(DiscoveredUrl.objects.filter(
            job=self.job, normalized_url=self.url + "admissions"
        ).exists())
        self.assertIn(self.url + "admissions", self.crawler.queued)

    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    def test_redirect_to_link_local_metadata_address_is_refused(self, _mock_dns):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            self.crawler._fetch_and_parse(client, self.url, None, "", 0, None)

        row = DiscoveredUrl.objects.get(job=self.job, normalized_url=self.url)
        self.assertEqual(calls, [self.url])
        self.assertIn("Blocked unsafe redirect target", row.exclusion_reason or "")
        self.assertIn("169.254.169.254", row.exclusion_reason or "")

    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    def test_bad_certificate_is_marked_failed_without_insecure_retry(self, _mock_dns):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            raise httpx.ConnectError("certificate verify failed", request=request)

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            self.crawler._fetch_and_parse(client, self.url, None, "", 0, None)

        row = DiscoveredUrl.objects.get(job=self.job, normalized_url=self.url)
        self.assertEqual(calls, [self.url])
        self.assertIn("certificate verify failed", (row.exclusion_reason or "").lower())

    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    def test_response_larger_than_five_megabytes_is_rejected(self, _mock_dns):
        def handler(request):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"x" * (5 * 1024 * 1024 + 1),
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            self.crawler._fetch_and_parse(client, self.url, None, "", 0, None)

        row = DiscoveredUrl.objects.get(job=self.job, normalized_url=self.url)
        self.assertIn("byte limit", row.exclusion_reason or "")
