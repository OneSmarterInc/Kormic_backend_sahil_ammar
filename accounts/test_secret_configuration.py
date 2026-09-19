import os
import subprocess
import sys
from pathlib import Path
from django.test import SimpleTestCase


class SecretConfigurationTests(SimpleTestCase):
    def test_missing_production_secret_fails_before_application_start(self):
        script = "from unittest.mock import patch; import runpy\nwith patch('dotenv.load_dotenv'):\n runpy.run_path('kormic_backend/settings.py')"
        env = {**os.environ, 'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': '', 'EMAIL_MODE': 'dev'}
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DJANGO_SECRET_KEY must be set', result.stderr)

    def test_explicit_secret_starts_with_debug_disabled(self):
        script = "from unittest.mock import patch; import runpy\nwith patch('dotenv.load_dotenv'):\n settings=runpy.run_path('kormic_backend/settings.py')\n assert settings['SECRET_KEY'] == 'isolated-configuration-test-key'"
        env = {**os.environ, 'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': 'isolated-configuration-test-key', 'EMAIL_MODE': 'dev'}
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
