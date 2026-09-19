import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch
spec = importlib.util.spec_from_file_location("config", Path(__file__).with_name("configure_environment.py"))
config = importlib.util.module_from_spec(spec); spec.loader.exec_module(config)

class EnvironmentTests(unittest.TestCase):
    def test_all_apps_share_one_local_api(self):
        matrix = json.loads((Path(__file__).resolve().parents[1] / "deploy/environments.json").read_text())
        result = config.render("local", config.resolve("local", matrix))
        self.assertEqual(result["student"]["EXPO_PUBLIC_API_BASE_URL"], "http://localhost:8000/api/v1")
        for role in ("superuser", "university", "institute"):
            self.assertEqual(result[role]["VITE_API_BASE_URL"], "http://localhost:8000")
        self.assertEqual(result["backend"]["GITHUB_OAUTH_REDIRECT_URI"], "http://localhost:8000/api/v1/auth/github/callback/")
        origins = result["backend"]["DJANGO_CORS_ALLOWED_ORIGINS"].split(",")
        self.assertIn("http://localhost:8081", origins)
        self.assertIn("http://127.0.0.1:8081", origins)
    def test_deployed_configuration_rejects_unresolved_or_insecure_origins(self):
        for origin in ("${MISSING_KORMIC_ORIGIN}", "http://localhost", "https://example.com/api", "https://user:secret@example.com"):
            with self.assertRaises(ValueError): config.resolve("staging", {"staging": {"api": origin}})

if __name__ == "__main__": unittest.main()
