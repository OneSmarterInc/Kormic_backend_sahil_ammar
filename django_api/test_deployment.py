import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from django.conf import settings
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from django_api.management.commands.check_deployment import Command


class DeploymentTests(SimpleTestCase):
    def test_compose_has_no_public_database_model_or_source_mount(self):
        config = yaml.safe_load((Path(settings.BASE_DIR) / 'compose.aws.yml').read_text())
        services = config['services']
        for name, service in services.items():
            if name != 'web':
                self.assertFalse(service.get('ports'), name)
            self.assertNotIn('.:/app', service.get('volumes', []))
            self.assertNotIn('container_name', service)
        self.assertTrue(services['web']['ports'][0].startswith('127.0.0.1:'))
        self.assertIn('X-Forwarded-Proto', str(services['web']['healthcheck']))
        for name in ('web', 'agent_worker', 'background_worker', 'celery_worker',
                     'university_worker', 'github_worker', 'celery_beat'):
            self.assertEqual(services[name]['depends_on']['migrate']['condition'],
                             'service_completed_successfully')
            self.assertEqual(services[name]['depends_on']['model_init']['condition'],
                             'service_completed_successfully')

    def run_probe(self, fail_queue=False):
        module = 'django_api.management.commands.check_deployment'
        cache = MagicMock()
        cache.get.side_effect = lambda key: key
        requests = []

        def send_task(name, args, **kwargs):
            result = MagicMock()
            result.get.return_value = args[0]
            if fail_queue:
                result.get.side_effect = TimeoutError()
            requests.append((name, kwargs['queue'], result))
            return result

        with patch(module + '.connection') as db, patch('redis.Redis.from_url'), \
                patch(module + '.caches', {'default': cache, 'agent_config': cache}), \
                patch(module + '.app.send_task', side_effect=send_task), \
                patch(module + '.urlopen') as http, \
                patch('django_api.management.commands.check_backend_ready.check_backend_ready'):
            db.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps({
                'models': [{'name': settings.GITHUB_OLLAMA_MODEL}]}).encode())
            Command(stdout=io.StringIO()).handle(model=False, timeout=1)
        return requests

    def test_probe_executes_every_queue_and_cleans_result_keys(self):
        calls = self.run_probe()
        self.assertEqual([row[1] for row in calls],
                         ['celery', 'agent_chat', 'agent_documents', 'knowledge_index'])
        for name, _, result in calls:
            self.assertEqual(name, 'kormic.deployment_probe')
            result.forget.assert_called_once()

    def test_probe_fails_when_worker_does_not_execute_task(self):
        with self.assertRaisesMessage(CommandError, 'celery did not complete'):
            self.run_probe(fail_queue=True)
