"""An API-origin setting must not replace the mobile invitation host."""
import os
import subprocess
import sys

from django.test import SimpleTestCase


class AppLinkSettingsTests(SimpleTestCase):
    def test_explicit_claim_host_survives_frontend_api_configuration(self):
        env = os.environ.copy()
        env.update({
            "DJANGO_DEBUG": "true",
            "DB_ENGINE": "sqlite",
            "EMAIL_MODE": "dev",
            "CLAIM_PAGE_URL": "https://app.kormic.ai/claim",
        })
        code = """
from pathlib import Path
from unittest.mock import patch

original_exists = Path.exists
def exists(path):
    if path.name == '.env' and path.parent.name == 'kormic_frontend_ammar_sahil':
        return True
    return original_exists(path)

with patch.object(Path, 'exists', exists), patch('dotenv.dotenv_values', return_value={
    'KORMIC_API_ORIGIN_PUBLIC': 'https://backend.kormic.ai',
}):
    import kormic_backend.settings as settings
assert settings.KORMIC_PUBLIC_URL == 'https://backend.kormic.ai'
assert settings.CLAIM_PAGE_URL == 'https://app.kormic.ai/claim', settings.CLAIM_PAGE_URL
"""
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True,
            text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
