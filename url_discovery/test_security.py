import gzip
import socket
import ssl
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from .domain_policy import DomainPolicy, resolves_to_public_ip
from .safe_http import PublicClient, FetchRejected, decompress_gzip

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))]


class SecureDiscoveryTests(SimpleTestCase):
    def test_dns_failure_and_mixed_addresses_fail_closed_without_cache(self):
        with patch('socket.getaddrinfo', side_effect=[PUBLIC, socket.gaierror(), PUBLIC + PRIVATE]) as dns:
            self.assertTrue(resolves_to_public_ip('example.edu'))
            self.assertFalse(resolves_to_public_ip('example.edu'))
            self.assertFalse(resolves_to_public_ip('example.edu'))
            self.assertEqual(dns.call_count, 3)

    def test_rebinding_between_policy_and_connect_is_rejected(self):
        with patch('socket.getaddrinfo', side_effect=[PUBLIC, PRIVATE]), patch('socket.create_connection') as connect:
            with self.assertRaises(FetchRejected):
                PublicClient(policy=DomainPolicy('https://example.edu')).get('https://example.edu/')
            connect.assert_not_called()

    def setup_transport(self, status=200, headers=None, chunks=None):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = status
        response.getheaders.return_value = headers or [('Content-Type', 'text/html')]
        response.read1.side_effect = chunks or [b'<html>safe</html>', b'']
        return conn

    @patch('socket.getaddrinfo', return_value=PUBLIC)
    @patch('socket.create_connection')
    @patch('ssl.create_default_context')
    @patch('http.client.HTTPConnection')
    def test_pins_public_ip_preserves_tls_hostname_and_host_header(self, http, tls, connect, dns):
        http.return_value = self.setup_transport()
        result = PublicClient(policy=DomainPolicy('https://example.edu')).get('https://example.edu/path?q=1')
        self.assertEqual(result.content, b'<html>safe</html>')
        self.assertEqual(connect.call_args.args[0], ('93.184.216.34', 443))
        tls.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname='example.edu')
        self.assertEqual(http.call_args.args[:2], ('example.edu', 443))
        self.assertEqual(http.return_value.request.call_args.args[:2], ('GET', '/path?q=1'))

    @patch('socket.getaddrinfo', return_value=PUBLIC)
    @patch('socket.create_connection')
    @patch('ssl.create_default_context')
    @patch('http.client.HTTPConnection')
    def test_bad_certificate_never_retries_insecurely(self, http, tls, connect, dns):
        tls.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError('bad certificate')
        with self.assertRaisesRegex(FetchRejected, 'TLS_VERIFICATION_FAILED'):
            PublicClient(policy=DomainPolicy('https://example.edu')).get('https://example.edu/')
        self.assertEqual(connect.call_count, 1)
        http.return_value.request.assert_not_called()
        connect.return_value.close.assert_called()

    @patch('socket.getaddrinfo', return_value=PUBLIC)
    @patch('socket.create_connection')
    @patch('http.client.HTTPConnection')
    def test_redirects_to_metadata_other_domains_and_credentials_never_connect(self, http, connect, dns):
        for target in ['http://169.254.169.254/latest', 'http://localhost/', 'http://outside.test/', 'http://user:pass@example.edu/', 'http://example.edu:8080/']:
            with self.subTest(target=target):
                connect.reset_mock()
                http.return_value = self.setup_transport(302, [('Location', target)])
                with self.assertRaises(FetchRejected):
                    PublicClient(policy=DomainPolicy('http://example.edu')).get('http://example.edu/')
                self.assertEqual(connect.call_count, 1)

    @patch('socket.getaddrinfo', return_value=PUBLIC)
    @patch('socket.create_connection')
    @patch('http.client.HTTPConnection')
    def test_allowed_redirect_is_revalidated_and_loops_are_bounded(self, http, connect, dns):
        http.side_effect = [self.setup_transport(302, [('Location', '/final')]), self.setup_transport()]
        result = PublicClient(policy=DomainPolicy('http://example.edu')).get('http://example.edu/')
        self.assertEqual(str(result.url), 'http://example.edu/final')
        self.assertEqual(dns.call_count, 4)
        http.side_effect = None
        http.return_value = self.setup_transport(302, [('Location', '/')])
        with self.assertRaisesRegex(FetchRejected, 'REDIRECT_LOOP'):
            PublicClient(policy=DomainPolicy('http://example.edu')).get('http://example.edu/')

    @patch('socket.getaddrinfo', return_value=PUBLIC)
    @patch('socket.create_connection')
    @patch('http.client.HTTPConnection')
    def test_declared_and_chunked_body_limits(self, http, connect, dns):
        for headers, chunks in [([('Content-Length', '9')], None), ([], [b'12345', b'6789'])]:
            http.return_value = self.setup_transport(headers=headers, chunks=chunks)
            with self.assertRaisesRegex(FetchRejected, 'RESPONSE_TOO_LARGE'):
                PublicClient(policy=DomainPolicy('http://example.edu'), max_bytes=8).get('http://example.edu/')

    def test_compressed_sitemap_limit(self):
        with self.assertRaisesRegex(FetchRejected, 'RESPONSE_TOO_LARGE'):
            decompress_gzip(gzip.compress(b'x' * 10000), limit=100)
