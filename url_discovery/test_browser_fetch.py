import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from url_discovery.browser_fetch import PublicResourcePolicy, needs_rendering, render_html
from url_discovery.domain_policy import DomainPolicy
from url_discovery.safe_fetch import request_with_policy


class BrowserTransportTests(unittest.TestCase):
    def test_private_cdn_redirect_is_blocked_before_connection(self):
        requests = []
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data/'})
        with patch('url_discovery.browser_fetch.resolves_to_public_ip', side_effect=lambda host: host == 'cdn.example.edu'):
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(ValueError, 'unsafe redirect'):
                    request_with_policy(client, 'https://cdn.example.edu/app.js', PublicResourcePolicy())
        self.assertEqual(requests, ['https://cdn.example.edu/app.js'])

    def test_only_html_shells_trigger_browser(self):
        self.assertTrue(needs_rendering(b'<main></main><script src="app.js"></script>', 'text/html'))
        self.assertFalse(needs_rendering(b'%PDF...', 'application/pdf'))
        self.assertFalse(needs_rendering(('<main>' + 'Requirements ' * 40 + '</main><script></script>').encode(), 'text/html'))

    @unittest.skipUnless(os.environ.get('KORMIC_BROWSER_TEST') == '1', 'Requires installed Chromium')
    def test_real_chromium_renders_script_and_blocks_private_frame(self):
        fetched = []
        html = b'''<html><main id="content"></main><script>
        document.querySelector('#content').textContent = 'Rendered admission requirements';
        </script><iframe src="http://127.0.0.1/private"></iframe></html>'''
        def fixture(url, policy, timeout, max_bytes):
            self.assertEqual(url, 'https://example.edu/')
            fetched.append(url)
            return 200, {'content-type': 'text/html'}, html, url
        with patch('url_discovery.browser_fetch.fetch_resource', side_effect=fixture), \
             patch.object(DomainPolicy, 'is_allowed', return_value=True):
            result = asyncio.run(render_html('https://example.edu/', DomainPolicy('https://example.edu/')))
        self.assertIn(b'>Rendered admission requirements</main>', result[2])
        self.assertEqual(fetched, ['https://example.edu/'])
