from unittest import mock

import httpx
from django.test import TestCase

from universities.models import University
from url_discovery.crawler import DirectUniversityCrawler
from url_discovery.models import DiscoveredUrl, DiscoveryJob


class CrawlerTransportSecurityTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Security Test University")
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
