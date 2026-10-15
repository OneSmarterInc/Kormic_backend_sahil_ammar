#!/usr/bin/env bash
# Public deployment helper. Contains only the release certificate fingerprint.
# Run on the existing Kormic production server after reviewing this file.
set -euo pipefail
cd /var/www/Kormic_backend_sahil_ammar

./venv/bin/python - <<'PY'
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from dotenv import dotenv_values, set_key

fingerprint = 'AE:AC:EB:51:F7:F0:01:4C:11:34:98:15:26:6E:FF:57:AC:96:0A:4C:ED:98:63:37:5B:94:EB:91:BC:97:63:69'
env_path = Path('.env')
settings_path = Path('kormic_backend/settings.py')
if not env_path.is_file() or not settings_path.is_file():
    raise SystemExit('Expected existing production .env and settings.py; nothing changed.')
source = settings_path.read_text()
legacy = "os.environ['CLAIM_PAGE_URL'] = KORMIC_PUBLIC_URL + '/claim'"
replacement = "os.environ.setdefault('CLAIM_PAGE_URL', KORMIC_PUBLIC_URL + '/claim')"
updated = source.replace(legacy, replacement)
if legacy not in source and re.search(r"os\.environ\[['\"]CLAIM_PAGE_URL['\"]\]\s*=", source):
    raise SystemExit('Unrecognized CLAIM_PAGE_URL override: review settings.py before proceeding.')

values = dotenv_values(env_path)
fingerprints = [v.strip() for v in (values.get('APP_LINK_ANDROID_SHA256_FINGERPRINTS') or '').split(',') if v.strip()]
if any(not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}', value) for value in fingerprints):
    raise SystemExit('Existing Android certificate configuration is malformed; review it first.')
fingerprints = list(dict.fromkeys([value.upper() for value in fingerprints] + [fingerprint]))
hosts = [v.strip() for v in (values.get('DJANGO_ALLOWED_HOSTS') or 'localhost,127.0.0.1').split(',') if v.strip()]
hosts = list(dict.fromkeys(hosts + ['backend.kormic.ai', 'app.kormic.ai']))

stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
backup_dir = Path('backups/app-links')
backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
for path in (env_path, settings_path):
    backup = backup_dir / (path.name + '.before-app-links-' + stamp)
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(path.read_bytes())

if updated != source:
    settings_path.write_text(updated)
for name, value in {
    'APP_LINK_ANDROID_SHA256_FINGERPRINTS': ','.join(fingerprints),
    'CLAIM_PAGE_URL': 'https://app.kormic.ai/claim',
    'STUDENT_WEB_CLAIM_URL': '',
    'DJANGO_ALLOWED_HOSTS': ','.join(hosts),
}.items():
    set_key(str(env_path), name, value)
print('Configured release certificate and canonical invitation URL. Private backups are stored in backups/app-links/.')
PY

./venv/bin/python manage.py check
./venv/bin/python manage.py shell <<'PY'
from django.conf import settings
from institutes_list.invitation_email import claim_link
expected = 'https://app.kormic.ai/claim?token=deployment-smoke-test'
if claim_link('deployment-smoke-test') != expected or settings.STUDENT_WEB_CLAIM_URL:
    raise SystemExit('Inherited environment overrides the app-link settings; correct it before restarting.')
print('New invitations use:', expected)
PY

# Run when active Celery tasks have finished; do not interrupt ongoing work.
sudo supervisorctl restart kormic:kormic-web kormic:kormic-celery
sudo supervisorctl status kormic:kormic-web kormic:kormic-celery

# Verify the public association, preserving TLS verification and rejecting redirects.
./venv/bin/python - <<'PY'
import json
from urllib.request import build_opener, HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None

fingerprint = 'AE:AC:EB:51:F7:F0:01:4C:11:34:98:15:26:6E:FF:57:AC:96:0A:4C:ED:98:63:37:5B:94:EB:91:BC:97:63:69'
with build_opener(NoRedirect).open('https://app.kormic.ai/.well-known/assetlinks.json', timeout=20) as response:
    if response.status != 200 or response.headers.get_content_type() != 'application/json':
        raise SystemExit('Association must return HTTP 200 application/json.')
    statements = json.load(response)
if not any(
    statement.get('target', {}).get('namespace') == 'android_app'
    and statement.get('target', {}).get('package_name') == 'com.kormic.student'
    and fingerprint in statement.get('target', {}).get('sha256_cert_fingerprints', [])
    and 'delegate_permission/common.handle_all_urls' in statement.get('relation', [])
    for statement in statements
):
    raise SystemExit('Public association does not contain the release certificate.')
print('SUCCESS: Production publishes the Kormic release signing certificate.')
PY
