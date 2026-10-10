from unittest.mock import patch
from django.test import SimpleTestCase
from url_discovery.domain_policy import DomainPolicy


class CandidatePolicyTests(SimpleTestCase):
    def test_discovery_does_not_resolve_every_link(self):
        policy = DomainPolicy('https://www.franklin.edu/')
        with patch('url_discovery.domain_policy.resolves_to_public_ip') as resolve:
            self.assertTrue(policy.is_in_scope('https://www.franklin.edu/admissions'))
            self.assertFalse(policy.is_in_scope('https://example.com/page'))
            resolve.assert_not_called()

    def test_fetch_rechecks_dns_and_rejects_private_resolution(self):
        policy = DomainPolicy('https://www.franklin.edu/')
        with patch('url_discovery.domain_policy.resolves_to_public_ip', side_effect=[True, False]) as resolve:
            self.assertTrue(policy.is_allowed('https://www.franklin.edu/admissions'))
            self.assertFalse(policy.is_allowed('https://www.franklin.edu/admissions'))
            self.assertEqual(resolve.call_count, 2)

    def test_out_of_scope_hosts_and_local_urls_never_resolve(self):
        policy = DomainPolicy('https://www.franklin.edu/')
        with patch('url_discovery.domain_policy.resolves_to_public_ip') as resolve:
            for url in ['https://example.com', 'http://127.0.0.1', 'file:///tmp/test', 'http://localhost']:
                self.assertFalse(policy.is_allowed(url))
            resolve.assert_not_called()
