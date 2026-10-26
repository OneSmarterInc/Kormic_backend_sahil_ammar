import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase

from .models import CSPReport


class CollectorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(enforce_csrf_checks=True)
        self.report = {
            "document-uri": "https://platform.kormic.ai/student/login?token=SECRET",
            "effective-directive": "script-src-elem",
            "blocked-uri": "https://cdn.example.com/private?token=SECRET#SECRET",
            "script-sample": "SECRET",
        }

    def send(self, report=None):
        return self.client.post("/api/security/csp-report/", json.dumps({
            "csp-report": self.report if report is None else report,
        }), content_type="application/csp-report")

    def test_public_reports_are_aggregated_without_payload_secrets(self):
        self.assertEqual(self.send().status_code, 204)
        self.assertEqual(self.send().status_code, 204)
        row = CSPReport.objects.get()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.blocked_origin, "https://cdn.example.com")
        self.assertNotIn("SECRET", str(list(CSPReport.objects.values())))

    def test_modern_batch(self):
        response = self.client.post("/api/security/csp-report/", json.dumps([{
            "type": "csp-violation", "body": {
                "documentURL": "https://platform.kormic.ai/",
                "effectiveDirective": "script-src", "blockedURL": "inline",
            },
        }]), content_type="application/reports+json")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(CSPReport.objects.get().blocked_origin, "inline")

    def test_invalid_and_foreign_reports_do_not_write(self):
        for override in [
            {"document-uri": "https://attacker.example/"},
            {"document-uri": "https://platform.kormic.ai.attacker.example/"},
            {"effective-directive": ["script-src"]},
            {"effective-directive": "untrusted"},
        ]:
            self.assertEqual(self.send({**self.report, **override}).status_code, 204)
        self.assertFalse(CSPReport.objects.exists())

    def test_limits_and_method(self):
        url = "/api/security/csp-report/"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, "x", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post(url, "x", content_type="application/csp-report").status_code, 400)
        self.assertEqual(self.client.post(url, "x" * 16385, content_type="application/csp-report").status_code, 413)
        self.assertEqual(self.client.post(url, json.dumps([{}] * 21), content_type="application/reports+json").status_code, 400)

    def test_rate_limit_and_cache_failure_fail_closed(self):
        with patch("security_reports.views.cache.add", return_value=False), patch(
            "security_reports.views.cache.incr", return_value=121
        ):
            self.assertEqual(self.send().status_code, 429)
        with patch("security_reports.views.cache.add", side_effect=ConnectionError):
            self.assertEqual(self.send().status_code, 503)
        self.assertFalse(CSPReport.objects.exists())
