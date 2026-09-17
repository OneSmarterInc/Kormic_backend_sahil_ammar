"""Public, side-effect-free mobile association and invitation landing pages."""
import re
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe


def _association_response(payload, *, configured):
    response = JsonResponse(payload, safe=False, status=200 if configured else 503)
    response['Cache-Control'] = 'public, max-age=300' if configured else 'no-store'
    return response


@require_safe
def android_assetlinks(request):
    fingerprints = settings.APP_LINK_ANDROID_SHA256_FINGERPRINTS
    configured = bool(fingerprints) and all(
        re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}', value)
        for value in fingerprints
    )
    if not configured:
        return _association_response({'error': 'Android app signing is not configured.'}, configured=False)
    return _association_response([{
        'relation': ['delegate_permission/common.handle_all_urls'],
        'target': {
            'namespace': 'android_app',
            'package_name': 'com.kormic.student',
            'sha256_cert_fingerprints': fingerprints,
        },
    }], configured=True)


@require_safe
def apple_app_site_association(request):
    prefix = settings.APP_LINK_APPLE_APP_ID_PREFIX
    if not re.fullmatch(r'[A-Z0-9]{10}', prefix):
        return _association_response({'error': 'Apple app identifier is not configured.'}, configured=False)
    return _association_response({'applinks': {
        'apps': [],
        'details': [{
            'appID': f'{prefix}.com.kormic.student',
            'paths': ['/claim', '/claim/'],
        }],
    }}, configured=True)


def _store_url(value, hostname):
    try:
        parsed = urlsplit(value)
        return value if (
            parsed.scheme == 'https' and parsed.hostname == hostname
            and not parsed.username and not parsed.password and parsed.port in (None, 443)
        ) else ''
    except ValueError:
        return ''


@require_safe
def claim_landing(request):
    # Rendering a link never consumes a token or sends an OTP. Email scanners
    # and app-link crawlers must not trigger the authenticated claim workflow.
    tokens = request.GET.getlist('token')
    token = tokens[0].strip() if len(tokens) == 1 else ''
    if len(token) > 2048 or any(ord(char) < 32 for char in token):
        token = ''
    response = render(request, 'institutes_list/claim_landing.html', {
        'token': token,
        'app_url': 'kormicstudent://claim?' + urlencode({'token': token}) if token else '',
        'android_store_url': _store_url(settings.APP_LINK_ANDROID_STORE_URL, 'play.google.com'),
        'ios_store_url': _store_url(settings.APP_LINK_IOS_STORE_URL, 'apps.apple.com'),
    })
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Content-Security-Policy'] = (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    return response
