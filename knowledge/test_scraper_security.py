from unittest import mock

import httpx
from django.test import SimpleTestCase

from knowledge.scraper import fetch_page
from url_discovery.domain_policy import DomainPolicy


class KnowledgeScraperTransportSecurityTests(SimpleTestCase):
    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    @mock.patch("knowledge.scraper.console.print")
    def test_private_redirect_is_blocked_and_returns_empty(self, mock_print, _mock_dns):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )

        policy = DomainPolicy("https://security.example/", include_subdomains=True)
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            result = fetch_page(
                "https://security.example/admissions",
                domain_policy=policy,
                client=client,
            )

        self.assertEqual(result, "")
        self.assertEqual(calls, ["https://security.example/admissions"])
        logged = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Blocked unsafe redirect target", logged)
        self.assertIn("169.254.169.254", logged)

    @mock.patch("url_discovery.domain_policy.resolves_to_public_ip", return_value=True)
    @mock.patch("knowledge.scraper.console.print")
    def test_oversized_response_is_blocked(self, mock_print, _mock_dns):
        def handler(request):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"x" * (5 * 1024 * 1024 + 1),
                request=request,
            )

        policy = DomainPolicy("https://security.example/", include_subdomains=True)
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            result = fetch_page(
                "https://security.example/admissions",
                domain_policy=policy,
                client=client,
            )

        self.assertEqual(result, "")
        logged = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("byte limit", logged)
