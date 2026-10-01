from urllib.parse import urlencode

from django.test import SimpleTestCase, override_settings


@override_settings(STUDENT_WEB_CLAIM_URL="")
class AppLinkTests(SimpleTestCase):
    @override_settings(APP_LINK_ANDROID_SHA256_FINGERPRINTS=['AB:' * 31 + 'AB'])
    def test_android_association_is_public_json_without_redirect(self):
        response = self.client.get('/.well-known/assetlinks.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json()[0]['target'], {
            'namespace': 'android_app', 'package_name': 'com.kormic.student',
            'sha256_cert_fingerprints': ['AB:' * 31 + 'AB'],
        })
        self.assertEqual(response.json()[0]['relation'], ['delegate_permission/common.handle_all_urls'])

    def test_missing_or_invalid_android_signing_cannot_appear_verified(self):
        for values in ([], ['YOUR_FINGERPRINT'], ['AB:' * 31 + 'AB', 'bad']):
            with self.subTest(values=values), override_settings(APP_LINK_ANDROID_SHA256_FINGERPRINTS=values):
                response = self.client.get('/.well-known/assetlinks.json')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response['Cache-Control'], 'no-store')

    @override_settings(APP_LINK_APPLE_APP_ID_PREFIX='ABCDE12345')
    def test_apple_association_limits_links_to_claim_path(self):
        response = self.client.get('/.well-known/apple-app-site-association')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response.json(), {'applinks': {'apps': [], 'details': [{
            'appID': 'ABCDE12345.com.kormic.student', 'paths': ['/claim', '/claim/'],
        }]}})

    @override_settings(APP_LINK_APPLE_APP_ID_PREFIX='')
    def test_unconfigured_apple_association_fails_closed(self):
        self.assertEqual(self.client.get('/.well-known/apple-app-site-association').status_code, 503)

    @override_settings(APP_LINK_ANDROID_STORE_URL='', APP_LINK_IOS_STORE_URL='')
    def test_fallback_preserves_token_and_does_not_require_database(self):
        # SimpleTestCase forbids database use: opening an email link is read-only.
        for path in ('/claim', '/claim/'):
            response = self.client.get(path, {'token': 'invite+123/&'})
            self.assertContains(response, 'kormicstudent://claim?' + urlencode({'token': 'invite+123/&'}))
            self.assertContains(response, 'return to your invitation email')
            self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_fallback_escapes_untrusted_token(self):
        response = self.client.get('/claim', {'token': '<script>alert(1)</script>'})
        self.assertNotContains(response, '<script>')
        self.assertContains(response, '&lt;script&gt;')
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])

    def test_missing_duplicate_or_oversized_token_does_not_open_app(self):
        for query in ('', '?token=a&token=b', '?token=' + 'a' * 2049, '?token=a%00b'):
            response = self.client.get('/claim' + query)
            self.assertNotContains(response, 'href="kormicstudent:')
            self.assertContains(response, 'Open the original link')

    @override_settings(
        APP_LINK_ANDROID_STORE_URL='https://play.google.com/store/apps/details?id=com.kormic.student',
        APP_LINK_IOS_STORE_URL='https://apps.apple.com/app/id123456789',
    )
    def test_configured_store_links_are_rendered_without_invitation_token(self):
        response = self.client.get('/claim?token=private-invite')
        self.assertContains(response, 'href="https://play.google.com/store/apps/details?id=com.kormic.student"')
        self.assertContains(response, 'href="https://apps.apple.com/app/id123456789"')

    @override_settings(APP_LINK_ANDROID_STORE_URL='javascript:alert(1)', APP_LINK_IOS_STORE_URL='https://evil.example/')
    def test_unsafe_store_links_are_not_rendered(self):
        response = self.client.get('/claim')
        self.assertNotContains(response, 'javascript:')
        self.assertNotContains(response, 'evil.example')

    def test_public_pages_disallow_post(self):
        for path in ('/claim', '/.well-known/assetlinks.json', '/.well-known/apple-app-site-association'):
            self.assertEqual(self.client.post(path).status_code, 405)
